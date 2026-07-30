from __future__ import annotations

import json
from html import escape
from typing import Any


MAX_REQUEST_BYTES = 8_000


def _page() -> bytes:
    return b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Function Catalog Demo</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #f3f5f8; color: #18212f; }
    main { max-width: 680px; margin: 8vh auto; padding: 28px; background: white; border: 1px solid #dfe3ea; border-radius: 12px; }
    textarea { width: 100%; min-height: 130px; box-sizing: border-box; padding: 12px; }
    button { margin-top: 12px; padding: 10px 16px; cursor: pointer; }
    pre { white-space: pre-wrap; background: #f7f9fc; padding: 14px; min-height: 48px; }
  </style>
</head>
<body>
  <main>
    <p>Server-side function invocation</p>
    <h1>Demo Text Transform</h1>
    <form id="form">
      <label for="text">Text</label>
      <textarea id="text" required>Hello from Eidolon</textarea>
      <button type="submit">Call function</button>
    </form>
    <pre id="result" aria-live="polite">Ready.</pre>
  </main>
  <script>
    const form = document.querySelector("#form");
    const result = document.querySelector("#result");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      result.textContent = "Calling demo_text_transform...";
      const response = await fetch("./api/transform", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({text: document.querySelector("#text").value})
      });
      const payload = await response.json();
      result.textContent = response.ok
        ? `${payload.transformed_text}\nCharacters: ${payload.character_count}`
        : payload.error;
    });
  </script>
</body>
</html>"""


async def _request_body(receive: Any) -> bytes:
    body = bytearray()
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("Request is too large.")
        more = bool(message.get("more_body", False))
    return bytes(body)


async def _respond(send: Any, status: int, body: bytes, content_type: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _json(send: Any, status: int, payload: dict[str, Any]) -> None:
    await _respond(send, status, json.dumps(payload).encode(), "application/json; charset=utf-8")


def _call_transform(text: str) -> dict[str, Any]:
    from web_runtime_capabilities import call_function

    return call_function("demo_text_transform", {"text": text})


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    if scope["method"] == "GET" and scope.get("path") in {"/", "/index.html"}:
        await _respond(send, 200, _page(), "text/html; charset=utf-8")
        return
    if scope["method"] == "POST" and scope.get("path") == "/api/transform":
        try:
            payload = json.loads(await _request_body(receive))
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Enter some text.")
            result = _call_transform(text)
            await _json(send, 200, result)
        except (json.JSONDecodeError, ValueError) as exc:
            await _json(send, 400, {"error": escape(str(exc))})
        except Exception:
            await _json(send, 502, {"error": "The function call could not be completed."})
        return
    await _json(send, 404, {"error": "Not found"})
