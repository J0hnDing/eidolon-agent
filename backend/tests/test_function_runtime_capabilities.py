import inspect
import json
from io import BytesIO
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

import function_runtime_capabilities as capabilities
import function_runtime_relay as relay


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_function_helper_discovers_and_invokes_with_ephemeral_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    def fake_urlopen(request, timeout: float):
        captured.append((request, timeout))
        if request.method == "GET":
            return FakeResponse([{"name": "normalize_text", "availability": "available"}])
        if request.full_url.endswith("/functions/capabilities/codex"):
            return FakeResponse(
                {
                    "response": "Bounded analysis.",
                    "model": "gpt-test",
                    "internet_access": False,
                }
            )
        return FakeResponse(
            {
                "run": {"id": 9, "status": "succeeded"},
                "output": {"result": "normalized"},
                "error": None,
            }
        )

    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://function-relay:8000")
    monkeypatch.setenv("PERSONAL_AGENT_FUNCTION_CAPABILITY", "run-secret")
    monkeypatch.setattr(capabilities, "urlopen", fake_urlopen)

    discovered = capabilities.discover_functions(timeout_seconds=4)
    output = capabilities.call_function("normalize_text", {"value": "Hello"}, timeout_seconds=7)
    codex_output = capabilities.call_codex(
        "Analyze this.",
        context={"item": "demo"},
        model="gpt-test",
        response_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        timeout_seconds=9,
    )

    assert discovered[0]["name"] == "normalize_text"
    assert output == {"result": "normalized"}
    assert codex_output["response"] == "Bounded analysis."
    assert captured[0][0].headers["Authorization"] == "Bearer run-secret"
    assert captured[0][0].full_url.endswith("/functions")
    assert captured[1][0].full_url.endswith("/functions/normalize_text/invoke")
    assert captured[2][0].full_url.endswith("/functions/capabilities/codex")
    assert captured[2][0].headers["Authorization"] == "Bearer run-secret"
    assert json.loads(captured[2][0].data) == {
        "prompt": "Analyze this.",
        "context": {"item": "demo"},
        "model": "gpt-test",
        "response_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
        "codex_permissions": {
            "call_response": True,
            "internet_access": False,
        },
    }
    assert captured[2][1] == 9
    assert "run-secret" not in captured[1][0].data.decode("utf-8")


def test_function_helper_reports_blocked_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://localhost:8000")
    monkeypatch.setenv("PERSONAL_AGENT_FUNCTION_CAPABILITY", "run-secret")

    def reject(request, timeout: float):
        del timeout
        raise HTTPError(request.full_url, 409, "Conflict", {}, BytesIO(b'{"detail":"not declared"}'))

    monkeypatch.setattr(capabilities, "urlopen", reject)

    with pytest.raises(capabilities.FunctionRuntimeCapabilityError, match="rejected"):
        capabilities.call_function("undeclared", {})


def test_function_codex_helper_rejects_empty_prompt() -> None:
    with pytest.raises(capabilities.FunctionRuntimeCapabilityError, match="cannot be empty"):
        capabilities.call_codex("   ")


def test_nested_skill_capabilities_use_five_minute_timeouts() -> None:
    assert capabilities.DEFAULT_SKILL_CAPABILITY_TIMEOUT_SECONDS == 300
    assert inspect.signature(capabilities.call_codex).parameters["timeout_seconds"].default == 300
    assert inspect.signature(capabilities.call_function).parameters["timeout_seconds"].default == 300
    assert relay.UPSTREAM_TIMEOUT_SECONDS == 300


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/invocation-approvals"),
        ("POST", "/invocation-approvals/1/approve"),
        ("GET", "/permission-requests"),
        ("POST", "/permission-requests/1/approve"),
        ("GET", "/settings"),
        ("GET", "/skills"),
        ("GET", "/agents"),
    ],
)
def test_function_relay_does_not_proxy_control_plane_paths(method: str, path: str) -> None:
    response = TestClient(relay.app).request(
        method,
        path,
        headers={"Authorization": "Bearer malicious-skill-capability"},
    )

    assert response.status_code == 404
