import asyncio
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("counter_app", ROOT / "app.py")
counter_app = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(counter_app)


def request(path, method="GET"):
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": method, "path": path}
    asyncio.run(counter_app.app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], body


def use_temporary_state(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(counter_app, "CACHE_DIR", cache)
    monkeypatch.setattr(counter_app, "STATE_FILE", cache / "counter.json")
    return cache / "counter.json"


def test_ui_and_assets_are_served_from_package():
    status, html = request("/")
    assert status == 200
    assert b"Persistent Counter" in html
    assert b'id="increment"' in html
    assert request("/static/styles.css")[0] == 200
    assert request("/static/app.js")[0] == 200


def test_increment_persists_and_is_restored(tmp_path, monkeypatch):
    state_file = use_temporary_state(tmp_path, monkeypatch)

    assert json.loads(request("/api/count")[1]) == {"count": 0}
    assert json.loads(request("/api/increment", "POST")[1]) == {"count": 1}
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"count": 1}

    # A fresh read from disk models restoration after a process restart.
    assert json.loads(request("/api/count")[1]) == {"count": 1}
    assert json.loads(request("/api/increment", "POST")[1]) == {"count": 2}


def test_invalid_state_falls_back_to_zero(tmp_path, monkeypatch):
    state_file = use_temporary_state(tmp_path, monkeypatch)
    state_file.parent.mkdir()
    state_file.write_text('{"count": "broken"}', encoding="utf-8")

    assert json.loads(request("/api/count")[1]) == {"count": 0}
    assert json.loads(request("/api/increment", "POST")[1]) == {"count": 1}
