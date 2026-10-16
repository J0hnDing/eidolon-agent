from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services import act_browser_runtime_service as browser


class RunningOwner:
    def poll(self):
        return None


def test_one_owner_multiple_conversations_and_token_rotation(monkeypatch, tmp_path):
    manager = browser.ActBrowserRuntimeService()
    starts = []
    requests = []

    def start(root):
        if manager._process is None:
            starts.append(root)
            manager._process = RunningOwner()
            manager._endpoint = "private-endpoint"
            manager._owner_token = "master-token"

    def request(endpoint, token, payload):
        requests.append((endpoint, token, payload))
        return {"ok": True}

    monkeypatch.setattr(manager, "_start", start)
    monkeypatch.setattr(browser, "browser_runtime_request", request)
    assert manager.register_session(1, "first-turn", tmp_path) == "private-endpoint"
    manager.register_session(2, "other-session", tmp_path)
    manager.revoke_session(1)
    manager.register_session(1, "next-turn", tmp_path)
    manager.close_session(2)
    assert starts == [tmp_path]
    assert [item[2] for item in requests] == [
        {"action": "register", "sessionId": 1, "sessionToken": "first-turn"},
        {"action": "register", "sessionId": 2, "sessionToken": "other-session"},
        {"action": "revoke", "sessionId": 1},
        {"action": "register", "sessionId": 1, "sessionToken": "next-turn"},
        {"action": "close_session", "sessionId": 2},
    ]
    assert all(item[:2] == ("private-endpoint", "master-token") for item in requests)


def test_control_requests_do_not_hold_global_lock(monkeypatch, tmp_path):
    manager = browser.ActBrowserRuntimeService()
    manager._process = RunningOwner()
    manager._endpoint = "endpoint"
    manager._owner_token = "master"
    rendezvous = threading.Barrier(2)

    def request(*args):
        rendezvous.wait(timeout=3)
        return {"ok": True}

    monkeypatch.setattr(browser, "browser_runtime_request", request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(manager.register_session, session, "token", tmp_path) for session in (1, 2)]
        assert [result.result(timeout=5) for result in results] == ["endpoint", "endpoint"]


@pytest.mark.parametrize("response", [b"not-json\n", b"[]\n", b'{"ok":true}', b""])
def test_private_pipe_errors_are_sanitized(monkeypatch, response):
    monkeypatch.setattr(browser, "_pipe_request", lambda *args: response)
    monkeypatch.setattr(browser.os, "name", "nt")
    with pytest.raises(browser.ActBrowserRuntimeError) as error:
        browser.browser_runtime_request(r"\\.\pipe\private", "password-token", {"password": "secret-password"})
    assert str(error.value) == "Shared Act browser is unavailable"


def test_request_envelope_and_response(monkeypatch):
    received = []

    def request(endpoint, data, timeout):
        received.append((endpoint, json.loads(data), timeout))
        return b'{"content":[],"isError":false}\n'

    monkeypatch.setattr(browser, "_pipe_request", request)
    monkeypatch.setattr(browser.os, "name", "nt")
    assert browser.browser_runtime_request(r"\\.\pipe\private", "real-token", {
        "action": "mcp_call", "token": "spoofed-token", "sessionId": "2",
    }, timeout=180) == {"content": [], "isError": False}
    assert received == [(r"\\.\pipe\private", {
        "action": "mcp_call", "token": "real-token", "sessionId": "2",
    }, 180)]


def test_cached_module_discovery_and_missing_package(monkeypatch, tmp_path):
    monkeypatch.delenv("EIDOLON_PLAYWRIGHT_MODULE_PATH", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("npm_config_cache", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    with pytest.raises(browser.ActBrowserRuntimeError, match="already installed"):
        browser.playwright_module_path()
    module = tmp_path / "_npx" / "cache" / "node_modules" / "@playwright" / "mcp" / "index.js"
    module.parent.mkdir(parents=True)
    module.write_text("module.exports = {}", encoding="utf-8")
    assert browser.playwright_module_path() == module.resolve()


def test_explicit_module_path_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("EIDOLON_PLAYWRIGHT_MODULE_PATH", str(tmp_path / "absent.js"))
    with pytest.raises(browser.ActBrowserRuntimeError, match="Configured Playwright"):
        browser.playwright_module_path()


def test_owner_launch_has_independent_stdin_and_private_shared_profile(monkeypatch, tmp_path):
    captured = {}

    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b'{"ready":true}\n')
            self.stderr = io.BytesIO(b"sensitive provider detail must be discarded")

        def poll(self):
            return None

        def wait(self, timeout):
            assert self.stdin.closed
            return 0

    process = Process()

    def launch(command, **kwargs):
        captured.update(command=command, **kwargs)
        return process

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(browser.shutil, "which", lambda name: "node.exe")
    monkeypatch.setattr(browser, "playwright_module_path", lambda: tmp_path / "index.js")
    monkeypatch.setattr(browser.subprocess, "Popen", launch)
    monkeypatch.setenv("EIDOLON_AGENT_TOKEN", "agent-secret")
    monkeypatch.setenv("EIDOLON_BROWSER_AUTH_TOKEN", "old-turn-secret")
    monkeypatch.setattr(browser, "browser_runtime_request", lambda *args: {"ok": True})
    manager = browser.ActBrowserRuntimeService()
    endpoint = manager.register_session(1, "turn-token", tmp_path / "workspace")
    assert endpoint
    assert process.stdin.closed is False
    assert captured["env"]["EIDOLON_BROWSER_PROFILE"] == str(tmp_path / "home/.eidolon/browser-profiles/act-shared")
    assert "EIDOLON_AGENT_TOKEN" not in captured["env"]
    assert "EIDOLON_BROWSER_AUTH_TOKEN" not in captured["env"]
    assert "shell" not in captured
    manager.revoke_session(1)
    manager.close_session(1)
    assert process.stdin.closed is False
    manager.stop()
    assert process.stdin.closed is True
