import asyncio
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("demo_text_transform_app", ROOT / "app.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def request(method, path, payload=None):
    sent = []
    received = False
    body = b"" if payload is None else json.dumps(payload).encode()

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(MODULE.app({"type": "http", "method": method, "path": path}, receive, send))
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    response = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, response


def test_page_and_server_side_function_call(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "_call_transform",
        lambda text: {"transformed_text": text.upper(), "character_count": len(text)},
    )

    assert request("GET", "/")[0] == 200
    status, body = request("POST", "/api/transform", {"text": "hello"})
    assert status == 200
    assert json.loads(body) == {"transformed_text": "HELLO", "character_count": 5}
