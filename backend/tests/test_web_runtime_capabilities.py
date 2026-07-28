import asyncio
import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi import Request

import web_runtime_capabilities as capabilities
import web_runtime_relay as relay
from app.services.skill_package_files import (
    SkillPackageFileError,
    read_skill_text,
    readable_skill_paths,
    snapshot_skill_files,
)


class FakeCapabilityResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_trusted_capability_helper_uses_instance_bearer_without_returning_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_urlopen(request, timeout: float):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeCapabilityResponse({"response": "done", "model": None, "internet_access": False})

    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://host.docker.internal:8000")
    monkeypatch.setenv("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "instance-secret")
    monkeypatch.setattr(capabilities, "urlopen", fake_urlopen)

    result = capabilities.call_codex("Summarize", context={"item": "demo"}, timeout_seconds=9)

    assert result == {"response": "done", "model": None, "internet_access": False}
    assert captured["timeout"] == 9
    request = captured["request"]
    assert request.full_url.endswith("/web-apps/capabilities/codex")
    assert request.headers["Authorization"] == "Bearer instance-secret"
    assert "instance-secret" not in request.data.decode("utf-8")


def test_trusted_capability_helper_reports_backend_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://localhost:8000")
    monkeypatch.setenv("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "instance-secret")

    def reject(request, timeout: float):
        del timeout
        raise HTTPError(request.full_url, 409, "Conflict", {}, BytesIO(b'{"detail":"blocked"}'))

    monkeypatch.setattr(capabilities, "urlopen", reject)

    with pytest.raises(capabilities.WebRuntimeCapabilityError, match="rejected"):
        capabilities.call_codex("Summarize")


def test_web_capability_helper_invokes_declared_function(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout: float):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeCapabilityResponse(
            {
                "run": {"id": 7, "status": "succeeded"},
                "output": {"result": "normalized"},
                "error": None,
            }
        )

    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://function-relay:8001")
    monkeypatch.setenv("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "instance-secret")
    monkeypatch.setattr(capabilities, "urlopen", fake_urlopen)

    result = capabilities.call_function("normalize_text", {"value": "Hello"}, timeout_seconds=8)

    assert result == {"result": "normalized"}
    assert captured["timeout"] == 8
    assert captured["request"].full_url.endswith("/web-apps/capabilities/functions/normalize_text")
    assert captured["request"].headers["Authorization"] == "Bearer instance-secret"


def test_skill_package_text_snapshot_includes_web_assets_but_excludes_cache_and_dependencies(tmp_path: Path) -> None:
    (tmp_path / "static").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / ".deps").mkdir()
    (tmp_path / "app.py").write_text("app = object()", encoding="utf-8")
    (tmp_path / "static" / "app.js").write_text("console.log('owned')", encoding="utf-8")
    (tmp_path / "cache" / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".deps" / "vendor.py").write_text("secret = True", encoding="utf-8")

    assert readable_skill_paths(tmp_path) == ["app.py", "static/app.js"]
    assert snapshot_skill_files(tmp_path)["static/app.js"] == "console.log('owned')"
    assert read_skill_text(tmp_path, "static/app.js") == "console.log('owned')"
    with pytest.raises(SkillPackageFileError):
        read_skill_text(tmp_path, "../outside.py")


def test_private_relay_exposes_only_scoped_backend_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_read_backend(request):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = request.data
        return b'{"response":"ok"}', 200, "application/json"

    monkeypatch.setattr(relay, "BACKEND_URL", "http://host.docker.internal:8000")
    monkeypatch.setattr(relay, "_read_backend", fake_read_backend)
    body = b'{"prompt":"hello"}'

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": relay.CODEX_CAPABILITY_PATH,
        "raw_path": relay.CODEX_CAPABILITY_PATH.encode("ascii"),
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer instance-token"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("relay", 8001),
    }

    response = asyncio.run(relay.forward_codex_capability(Request(scope, receive)))

    assert response.status_code == 200
    assert response.body == b'{"response":"ok"}'
    assert captured == {
        "url": "http://host.docker.internal:8000/web-apps/capabilities/codex",
        "authorization": "Bearer instance-token",
        "body": body,
    }
    assert {
        route.path
        for route in relay.capability_app.routes
        if "POST" in getattr(route, "methods", set())
    } == {
        relay.CODEX_CAPABILITY_PATH,
        "/web-apps/capabilities/functions/{function_name}",
        "/web-apps/capabilities/integrations/invoke",
    }
