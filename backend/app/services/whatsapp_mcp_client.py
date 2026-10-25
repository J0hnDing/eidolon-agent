"""MCP transport to the sole backend-owned WhatsApp runtime.

The backend reconstructs caller identity from the agent credential (or its
enabled local public MCP connection), then runs the ordinary invocation kernel.
No bridge credentials, serialized authority, or approval bypass cross this API.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.execution.types import InvocationExecutionError, InvocationOutcome

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
BACKEND_URL = "http://127.0.0.1:8000/integrations/whatsapp/mcp/invoke"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class WhatsAppMcpClient:
    def invoke(self, operation: str, input_json: dict[str, Any], *, agent_token: str | None) -> InvocationOutcome:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if agent_token is not None:
            headers["Authorization"] = f"Bearer {agent_token}"
        request = urllib.request.Request(
            BACKEND_URL,
            data=json.dumps({"operation": operation, "input": input_json}, separators=(",", ":")).encode(),
            headers=headers,
            method="POST",
        )
        # Never send an agent credential through an environment proxy or redirect.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(request, timeout=45)
        except urllib.error.HTTPError:
            raise InvocationExecutionError("backend_unavailable", "The WhatsApp backend could not accept this call") from None
        except (OSError, urllib.error.URLError, TimeoutError):
            raise InvocationExecutionError("backend_unavailable", "The WhatsApp backend is unavailable") from None
        try:
            with response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise InvocationExecutionError("response_too_large", "WhatsApp result exceeds the response limit")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("status") not in {"succeeded", "partial", "pending_approval", "failed"}:
                raise ValueError
            if not isinstance(value.get("output"), (dict, list, type(None))):
                raise ValueError
            if value.get("error_type") is not None and not isinstance(value["error_type"], str):
                raise ValueError
            return InvocationOutcome(**value)
        except (TypeError, ValueError, OSError):
            raise InvocationExecutionError("invalid_response", "The WhatsApp backend returned an invalid result") from None
