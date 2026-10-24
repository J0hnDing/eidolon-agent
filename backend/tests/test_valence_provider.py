import copy
import io
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler

import pytest
from jsonschema import Draft202012Validator

from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.valence import CALCULATE_INPUT, NATIVE_INPUT
from app.services.github_provider import IntegrationProviderError
from app.services.valence_provider import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, UrllibValenceProviderAdapter


def calculation():
    return {
        "person_id": "person-id", "model_version_id": "version-id",
        "actions": [{"id": "candidate", "action": {
            "title": "Encoded action", "duration_minutes": 60, "net_money_cost": -5,
            "features": dict.fromkeys(NATIVE_INPUT["action"]["properties"]["features"]["required"], 0),
        }}], "state": {"date": "2026-10-16", "mental_energy": None},
    }


def wire(monkeypatch, payload=None, *, raw=None, error=None):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            if error:
                raise error
            return io.BytesIO(raw if raw is not None else json.dumps(payload).encode())

    def build(*handlers):
        assert isinstance(handlers[0], ProxyHandler) and handlers[0].proxies == {}
        assert isinstance(handlers[1], HTTPRedirectHandler)
        assert handlers[1].redirect_request(None, None, 302, "", {}, "https://example.com") is None
        return Opener()

    monkeypatch.setattr("app.services.valence_provider.build_opener", build)
    return calls


def test_calculation_preserves_input_precision_and_only_posts_to_stateless_endpoint(monkeypatch):
    requested = calculation()
    requested["actions"][0]["action"]["features"]["mental_rest"] = .123456789012345
    requested["include_details"] = True
    expected = {"native": .123456789012345, "unknown": None}
    calls = wire(monkeypatch, expected)
    output = UrllibValenceProviderAdapter().execute(
        DEFAULT_INTEGRATION_REGISTRY.get("valence.utility.calculate"), requested,
    )
    assert output == expected
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8010/api/utility/calculate"
    assert request.get_method() == "POST" and timeout == 30
    assert json.loads(request.data) == requested


def test_describe_guides_exact_encodings_and_pages_people(monkeypatch):
    calls = wire(monkeypatch, [{"id": f"p-{index}"} for index in range(4)])
    provider = UrllibValenceProviderAdapter()
    spec = DEFAULT_INTEGRATION_REGISTRY.get("valence.describe")
    result = provider.execute(spec, {"section": "persons", "offset": 1, "limit": 2})
    assert result == {"description": {"persons": [{"id": "p-1"}, {"id": "p-2"}],
                                      "total": 4, "next_offset": 3}}
    assert calls[0][0].full_url.endswith("/api/persons")
    wire(monkeypatch, {"features": []})
    overview = provider.execute(spec, {})["description"]
    assert overview["input_schema"] == CALCULATE_INPUT
    assert overview["engine_version"] == "8"
    assert "zero is an assumption" in overview["guidance"]


@pytest.mark.parametrize("version,path", [(None, "model"), ("v-2", "model-versions/v-2")])
def test_model_discovery_resolves_latest_or_exact_version(monkeypatch, version, path):
    calls = wire(monkeypatch, {"model": {"exact": .123456789}, "id": version or "v-latest"})
    requested = {"section": "model", "person_id": "p-1"}
    if version:
        requested["model_version_id"] = version
    result = UrllibValenceProviderAdapter().execute(
        DEFAULT_INTEGRATION_REGISTRY.get("valence.describe"), requested,
    )
    assert result["description"]["model"]["exact"] == .123456789
    assert calls[0][0].full_url == f"http://127.0.0.1:8010/api/persons/p-1/{path}"


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(person_id="../secrets"),
    lambda value: value.update(url="https://example.com"),
    lambda value: value["state"].pop("date"),
    lambda value: value["state"].update(mental_energy=float("nan")),
    lambda value: value["state"].update(need_satisfaction={"social_contact": .5}),
    lambda value: value["actions"][0]["action"]["features"].pop("mental_rest"),
    lambda value: value["actions"][0]["action"]["features"].update(mental_rest=2),
    lambda value: value["actions"][0]["action"].update(feature_duration_minutes={"goal_progress": 5}),
    lambda value: value["actions"].append(copy.deepcopy(value["actions"][0])),
    lambda value: value["actions"][0].update(id=" "),
    lambda value: value.update(actions=value["actions"] * 26),
    lambda value: value.update(horizon_hours=8761),
])
def test_invalid_calculations_never_contact_provider(monkeypatch, mutate):
    calls = wire(monkeypatch, {})
    requested = calculation()
    mutate(requested)
    with pytest.raises(IntegrationProviderError) as error:
        UrllibValenceProviderAdapter().execute(
            DEFAULT_INTEGRATION_REGISTRY.get("valence.utility.calculate"), requested,
        )
    assert error.value.error_type == "invalid_input"
    assert not calls


@pytest.mark.parametrize("path", ["https://example.com", "//example.com", "/api/evaluations", "/api/persons/../model"])
def test_transport_rejects_arbitrary_paths_before_network(monkeypatch, path):
    calls = wire(monkeypatch, {})
    with pytest.raises(IntegrationProviderError, match="Unsupported Valence route"):
        UrllibValenceProviderAdapter()._request(path)
    assert not calls


@pytest.mark.parametrize("raw,expected", [
    (b"{credential-secret", "schema_mismatch"), (b'{"score": NaN}', "schema_mismatch"),
    (b'{"score": 1e999}', "schema_mismatch"), (b"null", "schema_mismatch"),
    (b" " * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
], ids=["invalid-json", "nan", "infinity", "null", "too-large"])
def test_malformed_nonfinite_and_oversized_responses_fail_without_body_leaks(monkeypatch, raw, expected):
    wire(monkeypatch, raw=raw)
    with pytest.raises(IntegrationProviderError) as error:
        UrllibValenceProviderAdapter()._request("/api/health")
    assert error.value.error_type == expected
    assert "credential-secret" not in str(error.value)


@pytest.mark.parametrize("status,expected", [(302, "provider_unavailable"), (404, "not_found"),
                                           (422, "invalid_input"), (500, "provider_unavailable")])
def test_http_errors_do_not_leak_provider_bodies(monkeypatch, status, expected):
    wire(monkeypatch, error=HTTPError("http://127.0.0.1:8010", status, "secret", {}, io.BytesIO(b"secret")))
    with pytest.raises(IntegrationProviderError) as error:
        UrllibValenceProviderAdapter()._request("/api/health")
    assert error.value.error_type == expected
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("exception,expected", [(TimeoutError("secret"), "provider_timeout"),
                                              (ConnectionResetError("secret"), "provider_unavailable"),
                                              (URLError("secret"), "provider_unavailable"),
                                              (URLError(TimeoutError("secret")), "provider_timeout")])
def test_network_errors_are_normalized(monkeypatch, exception, expected):
    wire(monkeypatch, error=exception)
    with pytest.raises(IntegrationProviderError) as error:
        UrllibValenceProviderAdapter()._request("/api/health")
    assert error.value.error_type == expected and "secret" not in str(error.value)


def test_request_size_is_checked_before_network(monkeypatch):
    calls = wire(monkeypatch, {})
    with pytest.raises(IntegrationProviderError) as error:
        UrllibValenceProviderAdapter()._request("/api/utility/calculate", payload={"large": "x" * MAX_REQUEST_BYTES})
    assert error.value.error_type == "invalid_input" and not calls


@pytest.mark.parametrize("payload,available", [({"status": "ok", "engine_version": "8"}, True),
                                            ({"status": "ok", "engine_version": "9"}, False),
                                            ({"status": "error", "engine_version": "8"}, False),
                                            ([], False)])
def test_availability_checks_compatible_engine_with_short_timeout(monkeypatch, payload, available):
    calls = wire(monkeypatch, payload)
    assert UrllibValenceProviderAdapter().available() is available
    assert calls[0][1] == 1


def test_documented_calculation_example_validates():
    spec = DEFAULT_INTEGRATION_REGISTRY.get("valence.utility.calculate")
    Draft202012Validator(spec.input_schema).validate(spec.presentation.usage_example["input"])
