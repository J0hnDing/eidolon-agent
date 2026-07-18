import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

import function_runtime_capabilities as capabilities


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

    assert discovered[0]["name"] == "normalize_text"
    assert output == {"result": "normalized"}
    assert captured[0][0].headers["Authorization"] == "Bearer run-secret"
    assert captured[0][0].full_url.endswith("/functions")
    assert captured[1][0].full_url.endswith("/functions/normalize_text/invoke")
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
