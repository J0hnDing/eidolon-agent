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
    """Call Codex through the instance-scoped Eidolon capability."""
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
        raise WebRuntimeCapabilityError(f"Eidolon capability rejected the request: {detail}") from exc
    except (OSError, URLError) as exc:
        raise WebRuntimeCapabilityError(f"Eidolon capability is unavailable: {exc}") from exc
    if len(raw) > MAX_CAPABILITY_RESPONSE_BYTES:
        raise WebRuntimeCapabilityError("Eidolon capability response exceeded the size limit")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebRuntimeCapabilityError("Eidolon capability returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("response"), str):
        raise WebRuntimeCapabilityError("Eidolon capability returned an invalid response contract")
    return result


def call_function(
    name: str,
    input_json: dict[str, Any],
    *,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Invoke one manifest-declared function through the instance capability."""
    if not name.strip():
        raise WebRuntimeCapabilityError("Function name cannot be empty")
    if not isinstance(input_json, dict):
        raise WebRuntimeCapabilityError("Function input must be a JSON object")
    backend_url = os.environ.get("PERSONAL_AGENT_BACKEND_URL", "").rstrip("/")
    token = os.environ.get("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "")
    if not backend_url or not token:
        raise WebRuntimeCapabilityError("Web application capability environment is unavailable")
    request = Request(
        f"{backend_url}/web-apps/capabilities/functions/{name}",
        data=json.dumps({"input": input_json}).encode("utf-8"),
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
        raise WebRuntimeCapabilityError(f"Eidolon capability rejected the request: {detail}") from exc
    except (OSError, URLError) as exc:
        raise WebRuntimeCapabilityError(f"Eidolon capability is unavailable: {exc}") from exc
    if len(raw) > MAX_CAPABILITY_RESPONSE_BYTES:
        raise WebRuntimeCapabilityError("Eidolon capability response exceeded the size limit")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebRuntimeCapabilityError("Eidolon capability returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("run"), dict):
        raise WebRuntimeCapabilityError("Eidolon capability returned an invalid function contract")
    run = result["run"]
    if run.get("status") not in {"succeeded", "partial"}:
        raise WebRuntimeCapabilityError(
            str(result.get("error") or run.get("error_message") or "Function invocation failed")
        )
    output = result.get("output")
    if not isinstance(output, dict):
        raise WebRuntimeCapabilityError("Function invocation did not return a JSON object")
    return output
