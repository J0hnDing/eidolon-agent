"""Trusted integration client exposed to bounded function entrypoints."""

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_CAPABILITY_RESPONSE_BYTES = 5_000_000


class IntegrationRuntimeCapabilityError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def call(
    *,
    operation: str,
    input: dict[str, Any],  # noqa: A002 - stable generated-code API
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Invoke one declared and approved integration operation."""
    if not operation.strip():
        raise IntegrationRuntimeCapabilityError("invalid_input", "Integration operation cannot be empty")
    if not isinstance(input, dict):
        raise IntegrationRuntimeCapabilityError("invalid_input", "Integration input must be a JSON object")
    backend_url = os.environ.get("PERSONAL_AGENT_BACKEND_URL", "").rstrip("/")
    token = os.environ.get("PERSONAL_AGENT_FUNCTION_CAPABILITY", "")
    if not backend_url or not token:
        raise IntegrationRuntimeCapabilityError(
            "authorization_missing_or_stale",
            "Function integration capability is unavailable",
        )
    request = Request(
        f"{backend_url}/integrations/capabilities/invoke",
        data=json.dumps({"operation": operation, "input": input}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "eidolon-function-integration-runtime",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_CAPABILITY_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raw_error = exc.read(16_000)
        error_type, message = _normalized_error(raw_error)
        raise IntegrationRuntimeCapabilityError(error_type, message) from None
    except (OSError, URLError):
        raise IntegrationRuntimeCapabilityError(
            "connection_unavailable",
            "Eidolon integration capability is unavailable",
        ) from None
    if len(raw) > MAX_CAPABILITY_RESPONSE_BYTES:
        raise IntegrationRuntimeCapabilityError("response_too_large", "Integration response exceeded the size limit")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrationRuntimeCapabilityError(
            "internal_failure",
            "Integration capability returned invalid JSON",
        ) from None
    output = result.get("output") if isinstance(result, dict) else None
    if not isinstance(output, dict):
        raise IntegrationRuntimeCapabilityError(
            "internal_failure",
            "Integration capability returned an invalid result",
        )
    return output


def _normalized_error(raw: bytes) -> tuple[str, str]:
    try:
        payload = json.loads(raw)
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            error_type = str(detail.get("type") or "internal_failure")
            message = str(detail.get("message") or "Integration request failed")
            return error_type[:64], message[:1000]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return "internal_failure", "Integration request failed"
