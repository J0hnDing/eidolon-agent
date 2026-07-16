"""Trusted, scoped capability client exposed to sandboxed web applications."""

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_CAPABILITY_RESPONSE_BYTES = 5_000_000


class WebRuntimeCapabilityError(RuntimeError):
    pass


def call_codex(
    prompt: str,
    *,
    context: dict[str, Any] | None = None,
    model: str | None = None,
    internet_access: bool = False,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Call Codex through the instance-scoped Personal Agent capability."""
    if not prompt.strip():
        raise WebRuntimeCapabilityError("Codex prompt cannot be empty")
    backend_url = os.environ.get("PERSONAL_AGENT_BACKEND_URL", "").rstrip("/")
    token = os.environ.get("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "")
    if not backend_url or not token:
        raise WebRuntimeCapabilityError("Web application capability environment is unavailable")
    payload: dict[str, Any] = {
        "prompt": prompt,
        "context": context or {},
        "codex_permissions": {
            "call_response": True,
            "internet_access": internet_access,
        },
    }
    if model is not None:
        payload["model"] = model
    request = Request(
        f"{backend_url}/web-apps/capabilities/codex",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "personal-agent-web-runtime",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_CAPABILITY_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(16_000).decode("utf-8", errors="replace")
        raise WebRuntimeCapabilityError(f"Personal Agent capability rejected the request: {detail}") from exc
    except (OSError, URLError) as exc:
        raise WebRuntimeCapabilityError(f"Personal Agent capability is unavailable: {exc}") from exc
    if len(raw) > MAX_CAPABILITY_RESPONSE_BYTES:
        raise WebRuntimeCapabilityError("Personal Agent capability response exceeded the size limit")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebRuntimeCapabilityError("Personal Agent capability returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("response"), str):
        raise WebRuntimeCapabilityError("Personal Agent capability returned an invalid response contract")
    return result
