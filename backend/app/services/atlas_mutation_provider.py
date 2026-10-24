"""Fixed-route native Atlas lifecycle and custom-field provider."""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from jsonschema import Draft202012Validator

from app.integrations.atlas_lifecycle import ATLAS_LIFECYCLE_OPERATIONS, ATLAS_LIFECYCLE_SPECS
from app.services.atlas_read_provider import _segment
from app.services.github_provider import IntegrationProviderError

_SPECS = {spec.id: spec for spec in ATLAS_LIFECYCLE_SPECS}
_MAX_NATIVE_BYTES = 32 * 1024 * 1024


def _error(kind: str = "invalid_input") -> IntegrationProviderError:
    return IntegrationProviderError(kind, "Atlas operation could not be completed")


class AtlasMutationProvider:
    def __init__(self, request: Callable[..., Any]) -> None:
        self.request = request

    def execute(self, operation_id: str, requested: dict) -> dict:
        if operation_id not in ATLAS_LIFECYCLE_OPERATIONS:
            raise _error()
        if not Draft202012Validator(_SPECS[operation_id].input_schema).is_valid(requested):
            raise _error()
        if operation_id == "atlas.custom_fields.update" and len(requested) < 2:
            raise _error()
        method, payload, output_key = "POST", None, "result"
        if operation_id.startswith("atlas.records."):
            action = operation_id.rsplit(".", 1)[1]
            path = f"/api/records/{_segment(requested['id'])}"
            payload = {"revision": requested["expected_revision"]}
            output_key = "record"
            if action == "trash":
                method = "DELETE"
            elif action == "restore":
                path += "/restore"
            else:
                path += f"/revisions/{requested['revision']}/restore"
                if "confirm_currency_change" in requested:
                    payload["confirmCurrencyChange"] = requested["confirm_currency_change"]
        elif operation_id == "atlas.trash.empty":
            method, path = "DELETE", "/api/trash"
        elif operation_id.startswith("atlas.links."):
            path = f"/api/links/{_segment(requested['id'])}"
            if operation_id == "atlas.links.delete":
                method, payload = "DELETE", {"revision": requested["expected_revision"]}
            else:
                path += "/restore-revision"
                payload = {"revision": requested["revision"], "expectedRevision": requested["expected_revision"]}
                output_key = "link"
        else:
            action = operation_id.rsplit(".", 1)[1]
            path = "/api/custom-fields"
            if action == "list":
                method, output_key = "GET", "fields"
                if "category" in requested:
                    path += "?" + urlencode({"category": requested["category"]})
            elif action == "create":
                payload, output_key = dict(requested), "field"
            else:
                path += "/" + _segment(requested["id"])
                if action == "update":
                    method, output_key = "PATCH", "field"
                    payload = {key: value for key, value in requested.items() if key != "id"}
                else:
                    method = "DELETE"
        try:
            result = self.request(path, payload, timeout=15, max_bytes=_MAX_NATIVE_BYTES, method=method)
        except IntegrationProviderError:
            raise
        except TimeoutError:
            raise _error("provider_timeout") from None
        except (OSError, ValueError, TypeError):
            raise _error("provider_unavailable") from None
        if output_key == "fields":
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise _error("provider_unavailable")
        elif not isinstance(result, dict):
            raise _error("provider_unavailable")
        elif output_key in {"record", "link", "field"} and not isinstance(result.get("id"), str):
            raise _error("provider_unavailable")
        return {output_key: result}
