"""Fixed-loopback transport for Valence's stateless native utility API."""

import json
import math
import re
import socket
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from jsonschema import Draft202012Validator, ValidationError

from app.integrations.types import IntegrationOperationSpec
from app.integrations.valence import CALCULATE_INPUT, NATIVE_INPUT
from app.services.github_provider import IntegrationProviderError

VALENCE_BASE = "http://127.0.0.1:8010"
MAX_REQUEST_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 5_000_000


class ValenceTransport(Protocol):
    def available(self) -> bool: ...

    def execute(self, operation: IntegrationOperationSpec, input_json: dict[str, Any]) -> dict: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


class UrllibValenceProviderAdapter:
    def available(self) -> bool:
        try:
            result = self._request("/api/health", timeout=1)
            return result.get("status") == "ok" and result.get("engine_version") == NATIVE_INPUT["engine_version"]
        except (IntegrationProviderError, AttributeError):
            return False

    def execute(self, operation: IntegrationOperationSpec, input_json: dict[str, Any]) -> dict:
        try:
            _finite(input_json)
            Draft202012Validator(operation.input_schema).validate(input_json)
        except (ValueError, ValidationError, RecursionError):
            raise IntegrationProviderError("invalid_input", "Invalid Valence input") from None
        if operation.id == "valence.utility.calculate":
            ids = [item["id"] for item in input_json["actions"]]
            if len(ids) != len(set(ids)) or any(not value.strip() for value in ids):
                raise IntegrationProviderError("invalid_input", "Action identifiers must be unique and nonblank")
            return self._request("/api/utility/calculate", payload=input_json)
        if operation.id != "valence.describe":
            raise IntegrationProviderError("operation_undeclared", "Unsupported Valence operation")
        section = input_json.get("section", "overview")
        if section == "persons":
            persons = self._request("/api/persons")
            if not isinstance(persons, list):
                raise IntegrationProviderError("schema_mismatch", "Invalid Valence persons response")
            offset, limit = input_json.get("offset", 0), input_json.get("limit", 25)
            end = offset + limit
            return {"description": {"persons": persons[offset:end], "total": len(persons),
                                    "next_offset": end if end < len(persons) else None}}
        if section == "model":
            person_id = input_json["person_id"]
            path = f"/api/persons/{person_id}/model"
            if version := input_json.get("model_version_id"):
                path = f"/api/persons/{person_id}/model-versions/{version}"
            return {"description": self._request(path)}
        return {"description": {
            "engine_version": NATIVE_INPUT["engine_version"], "input_schema": CALCULATE_INPUT,
            "ontology": self._request("/api/ontology"),
            "guidance": "Encode all 19 features explicitly; zero is an assumption, not unknown. "
            "Discover persons, then read the desired model. Supply current state and its date explicitly. "
            "Unknown energy is null and omitted needs contribute zero. Money uses the model's currency "
            "convention without conversion. Whole-action outcomes count once; rate features use exposure "
            "duration. Ranking uses total_utility; utility_per_action_hour excludes time cost and is null "
            "at zero duration. No records are saved. This optional tool is separate from assessment.",
        }}

    def _request(self, path: str, *, payload: dict | None = None, timeout: float = 30) -> Any:
        # No caller-controlled host, path, proxy or redirect destinations.
        if not re.fullmatch(
            r"/api/(health|ontology|persons|utility/calculate|persons/[A-Za-z0-9_-]+/"
            r"(model|model-versions/[A-Za-z0-9_-]+))", path,
        ):
            raise IntegrationProviderError("invalid_input", "Unsupported Valence route")
        try:
            data = json.dumps(payload, allow_nan=False).encode("utf-8") if payload is not None else None
            if data is not None and len(data) > MAX_REQUEST_BYTES:
                raise IntegrationProviderError("invalid_input", "Valence request exceeds the size limit")
            request = Request(VALENCE_BASE + path, data=data, headers={
                "Accept": "application/json", "Content-Type": "application/json",
            })
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise IntegrationProviderError("response_too_large", "Valence response exceeds the size limit")
            result = json.loads(raw)
            _finite(result)
            if not isinstance(result, (dict, list)):
                raise ValueError("Expected a JSON object or list")
            return result
        except HTTPError as exc:
            error_type = {404: "not_found", 422: "invalid_input", 400: "invalid_input"}.get(
                exc.code, "provider_unavailable",
            )
            exc.close()
            raise IntegrationProviderError(error_type, "Valence request failed safely") from None
        except (TimeoutError, socket.timeout):
            raise IntegrationProviderError("provider_timeout", "Valence request timed out") from None
        except URLError as exc:
            error_type = "provider_timeout" if isinstance(exc.reason, TimeoutError) else "provider_unavailable"
            raise IntegrationProviderError(error_type, "Valence is unavailable") from None
        except OSError:
            raise IntegrationProviderError("provider_unavailable", "Valence is unavailable") from None
        except (ValueError, UnicodeError, RecursionError):
            raise IntegrationProviderError("schema_mismatch", "Invalid Valence response") from None
