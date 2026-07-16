"""Trusted ASGI wrapper used inside the controlled web-application runtime."""

import importlib
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

AsgiApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict[str, Any]]], Callable[..., Awaitable[None]]], Awaitable[None]]


def _load_skill_app() -> AsgiApp:
    entrypoint = os.environ.get("PERSONAL_AGENT_WEB_ENTRYPOINT", "").strip()
    module_name, separator, attribute = entrypoint.partition(":")
    if separator != ":" or not module_name or not attribute:
        raise RuntimeError("PERSONAL_AGENT_WEB_ENTRYPOINT must use module:attribute syntax")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute, None)
    if not callable(target):
        raise RuntimeError(f"Web application entrypoint is not callable: {entrypoint}")
    return target


class TrustedWebRuntimeHost:
    def __init__(self) -> None:
        self.skill_app = _load_skill_app()

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/__personal_agent__/health":
            body = json.dumps({"status": "ready"}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.skill_app(scope, receive, send)


app = TrustedWebRuntimeHost()
