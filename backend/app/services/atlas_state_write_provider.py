"""Fixed native routes for States edits."""

import math
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from app.integrations.atlas_state_write import ATLAS_STATE_WRITE_SPECS
from app.services.github_provider import IntegrationProviderError

_SPECS = {spec.id: spec for spec in ATLAS_STATE_WRITE_SPECS}


def _finite_exact(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_finite_exact(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_exact(item) for item in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
            return math.isfinite(number) and (not isinstance(value, int) or int(number) == value)
        except (OverflowError, ValueError):
            return False
    return True


class AtlasStateWriteProvider:
    def __init__(self, request):
        self.request = request

    def execute(self, operation_id: str, input_json: dict[str, Any]) -> dict[str, Any]:
        spec = _SPECS.get(operation_id)
        if spec is None:
            raise IntegrationProviderError("internal_failure", "Atlas States operation is unsupported")
        if (not _finite_exact(input_json)
                or not Draft202012Validator(spec.input_schema, format_checker=FormatChecker()).is_valid(input_json)):
            raise IntegrationProviderError("invalid_input", "Atlas input does not match the native contract")
        if operation_id == "atlas.states.configuration.update":
            payload = {"revision": input_json["expected_revision"],
                       **{key: input_json[key] for key in ("states", "targets") if key in input_json}}
            result = self.request("/api/states/configuration", payload, timeout=10,
                                  max_bytes=2_000_000, method="PATCH")
            if not isinstance(result, dict) or not isinstance(result.get("states"), dict):
                raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid States configuration")
            return {"configuration": result}
        path = "/api/states/events" if operation_id == "atlas.states.events.create" else "/api/states/observations"
        result = self.request(path, input_json, timeout=10, max_bytes=2_000_000, method="POST")
        if (not isinstance(result, dict) or not isinstance(result.get("event"), dict)
                or not isinstance(result.get("duplicate"), bool)):
            raise IntegrationProviderError("provider_unavailable", "Atlas returned invalid States event data")
        return result
