"""Trusted capability client exposed to bounded function entrypoints."""

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_CAPABILITY_RESPONSE_BYTES = 5_000_000


class FunctionRuntimeCapabilityError(RuntimeError):
    pass


def call_codex(
    prompt: str,
    *,
    context: dict[str, Any] | None = None,
    model: str | None = None,
    internet_access: bool = False,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Call Codex through the current function run's scoped capability."""
    if not prompt.strip():
        raise FunctionRuntimeCapabilityError("Codex prompt cannot be empty")
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
    result = _request(
        "/functions/capabilities/codex",
        method="POST",
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(result, dict) or not isinstance(result.get("response"), str):
        raise FunctionRuntimeCapabilityError("Codex capability returned an invalid response contract")
    return result


def discover_functions(*, timeout_seconds: float = 10) -> list[dict[str, Any]]:
    """Discover installed functions without granting invocation authority."""
    result = _request("/functions", method="GET", payload=None, timeout_seconds=timeout_seconds)
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise FunctionRuntimeCapabilityError("Function registry returned an invalid discovery contract")
    return result


def call_function(
    name: str,
    input_json: dict[str, Any],
    *,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Invoke one manifest-declared function through the backend control plane."""
    if not name.strip():
        raise FunctionRuntimeCapabilityError("Function name cannot be empty")
    if not isinstance(input_json, dict):
        raise FunctionRuntimeCapabilityError("Function input must be a JSON object")
    result = _request(
        f"/functions/{name}/invoke",
        method="POST",
        payload={"input": input_json},
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(result, dict) or not isinstance(result.get("run"), dict):
        raise FunctionRuntimeCapabilityError("Function invocation returned an invalid response contract")
    run = result["run"]
    if run.get("status") not in {"succeeded", "partial"}:
        raise FunctionRuntimeCapabilityError(
            str(result.get("error") or run.get("error_message") or "Function invocation failed")
        )
    output = result.get("output")
    if not isinstance(output, dict):
        raise FunctionRuntimeCapabilityError("Function invocation did not return a JSON object")
    return output


def _request(
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> Any:
    backend_url = os.environ.get("PERSONAL_AGENT_BACKEND_URL", "").rstrip("/")
    token = os.environ.get("PERSONAL_AGENT_FUNCTION_CAPABILITY", "")
    if not backend_url or not token:
        raise FunctionRuntimeCapabilityError("Function caller capability environment is unavailable")
    request = Request(
        f"{backend_url}{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "personal-agent-function-runtime",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_CAPABILITY_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(16_000).decode("utf-8", errors="replace")
        raise FunctionRuntimeCapabilityError(f"Eidolon rejected the function request: {detail}") from exc
    except (OSError, URLError) as exc:
        raise FunctionRuntimeCapabilityError(f"Eidolon function capability is unavailable: {exc}") from exc
    if len(raw) > MAX_CAPABILITY_RESPONSE_BYTES:
        raise FunctionRuntimeCapabilityError("Function capability response exceeded the size limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionRuntimeCapabilityError("Function capability returned invalid JSON") from exc
