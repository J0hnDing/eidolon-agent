import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import function_runtime_relay
import integration_runtime_capabilities as capabilities
import web_runtime_capabilities as web_capabilities
from integration_test_adapter import DeterministicFakeIntegrationAdapter, FakeIntegrationError


class Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_function_helper_uses_existing_capability_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://relay")
    monkeypatch.setenv("PERSONAL_AGENT_FUNCTION_CAPABILITY", "runtime-capability")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response({"output": {"full_name": "octo/demo"}})

    monkeypatch.setattr(capabilities, "urlopen", fake_urlopen)
    output = capabilities.call(
        operation="github.repository.get",
        input={"owner": "octo", "repository": "demo"},
        timeout_seconds=7,
    )
    assert output == {"full_name": "octo/demo"}
    assert captured == {
        "url": "http://relay/integrations/capabilities/invoke",
        "authorization": "Bearer runtime-capability",
        "body": {
            "operation": "github.repository.get",
            "input": {"owner": "octo", "repository": "demo"},
        },
        "timeout": 7,
    }
    assert "runtime-capability" not in json.dumps(output)


def test_function_helper_returns_only_normalized_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://relay")
    monkeypatch.setenv("PERSONAL_AGENT_FUNCTION_CAPABILITY", "runtime-capability")
    payload = json.dumps(
        {"detail": {"type": "repository_outside_scope", "message": "Repository is outside scope"}}
    ).encode()
    error = HTTPError("http://relay", 409, "Conflict", {}, io.BytesIO(payload))
    monkeypatch.setattr(capabilities, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(capabilities.IntegrationRuntimeCapabilityError) as exc_info:
        capabilities.call(operation="github.repository.get", input={})
    assert exc_info.value.error_type == "repository_outside_scope"
    assert "runtime-capability" not in str(exc_info.value)


def test_web_helper_uses_instance_capability_and_normalized_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://relay")
    monkeypatch.setenv("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "instance-capability")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return Response({"output": {"repositories": []}})

    monkeypatch.setattr(web_capabilities, "urlopen", fake_urlopen)
    output = web_capabilities.call_integration(
        operation="github.repository.trending.list",
        input={"limit": 5},
    )
    assert output == {"repositories": []}
    assert captured["url"].endswith("/web-apps/capabilities/integrations/invoke")
    assert captured["authorization"] == "Bearer instance-capability"
    assert "instance-capability" not in json.dumps(output)


def test_no_network_function_relay_exposes_only_existing_scoped_capabilities() -> None:
    post_routes = {
        route.path
        for route in function_runtime_relay.app.routes
        if "POST" in getattr(route, "methods", set())
    }
    assert post_routes == {
        "/functions/{function_name}/invoke",
        "/integrations/capabilities/invoke",
    }


def test_generated_tests_use_deterministic_fake_without_a_credential() -> None:
    adapter = DeterministicFakeIntegrationAdapter(
        {"github.repository.get": {"full_name": "octo/demo"}},
        failures={"github.issue.list": ("rate_limited", "Fake rate limit")},
    )
    assert adapter.call(
        operation="github.repository.get",
        input={"owner": "octo", "repository": "demo"},
    ) == {"full_name": "octo/demo"}
    with pytest.raises(FakeIntegrationError) as exc_info:
        adapter.call(operation="github.issue.list", input={})
    assert exc_info.value.error_type == "rate_limited"
