"""Checked-in Valence engine-v8 integration contracts; no assessment hooks."""

import json
from pathlib import Path

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ResourceSpec,
    RiskLevel,
)

NATIVE_INPUT = json.loads(
    (Path(__file__).resolve().parents[1] / "static" / "valence_input_contract.json").read_text(encoding="utf-8")
)
IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"^[A-Za-z0-9_-]+$"}
ERRORS = (
    "connection_unavailable", "authorization_missing_or_stale", "invalid_input", "not_found",
    "provider_timeout", "response_too_large", "provider_unavailable", "schema_mismatch", "internal_failure",
)


def object_schema(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


CALCULATE_INPUT = object_schema({
    "person_id": IDENTIFIER,
    "model_version_id": IDENTIFIER,
    "actions": {"type": "array", "minItems": 1, "maxItems": 25, "items": object_schema({
        "id": {"type": "string", "minLength": 1, "maxLength": 100},
        "action": NATIVE_INPUT["action"],
    }, ("id", "action"))},
    "state": NATIVE_INPUT["state"],
    "horizon_hours": {"type": "number", "minimum": 0, "maximum": 8760, "default": 168},
    "include_details": {"type": "boolean", "default": False},
}, ("person_id", "actions", "state"))

_RESULT = {
    "type": "object",
    "properties": {
        "action_id": {"type": "string"}, "action_snapshot": {"type": "object"},
        "rank": {"type": "integer", "minimum": 1}, "total_utility": {"type": "number"},
        "utility_per_action_hour": {"type": ["number", "null"]},
    },
    "required": ["action_id", "action_snapshot", "rank", "total_utility", "utility_per_action_hour"],
}
CALCULATE_OUTPUT = object_schema({
    "engine_version": {"const": "8"}, "person_id": {"type": "string"},
    "model_version_id": {"type": "string"}, "model_version": {"type": "integer", "minimum": 1},
    "model_snapshot": {"type": "object"}, "converted_from_legacy": {"type": "boolean"},
    "state": {"type": "object"}, "horizon_hours": {"type": "number"},
    "results": {"type": "array", "minItems": 1, "maxItems": 25, "items": _RESULT},
}, ("engine_version", "person_id", "model_version_id", "model_version", "model_snapshot",
    "converted_from_legacy", "state", "horizon_hours", "results"))

_DESCRIBE_INPUT = object_schema({
    "section": {"enum": ["overview", "persons", "model"], "default": "overview"},
    "person_id": IDENTIFIER, "model_version_id": IDENTIFIER,
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
})
_DESCRIBE_INPUT["allOf"] = [
    {"if": {"properties": {"section": {"const": "model"}}, "required": ["section"]},
     "then": {"required": ["person_id"]},
     "else": {"not": {"anyOf": [{"required": ["person_id"]}, {"required": ["model_version_id"]}]}}},
]

VALENCE_SPECS = (
    IntegrationOperationSpec(
        id="valence.describe", title="Discover Valence utility inputs and persons",
        description="Read Valence's feature ontology and exact calculation input schema (section=overview), "
        "page saved persons (section=persons; follow next_offset), or read a saved Person Model "
        "(section=model, person_id, optional model_version_id; otherwise latest). No data is changed.",
        input_schema=_DESCRIBE_INPUT,
        output_schema=object_schema({"description": {"type": "object"}}, ("description",)),
        effects=frozenset({IntegrationEffect.READ}), resource=ResourceSpec("valence.model", ("person_id",)),
        risk=RiskLevel.LOW, presentation=OperationPresentation(
            usage_example={"operation": "valence.describe", "input": {}}, normalized_errors=ERRORS,
        ),
    ),
    IntegrationOperationSpec(
        id="valence.utility.calculate", title="Calculate utility with Valence",
        description="Explicit, stateless utility calculation for 1-25 encoded actions using a saved Valence "
        "Person Model and supplied transient state. Read valence.describe first. Supply all 19 canonical "
        "feature values, duration, net money cost and state date. Disclose encoding assumptions; titles/notes "
        "do not affect scores. Missing energy/need values remain unknown. Model version defaults to latest "
        "and is returned with its exact snapshot; pin model_version_id for reproducibility. Results rank by "
        "net total_utility, retaining native precision and contribution totals. include_details adds full "
        "traces/graph. No saved actions, states or evaluations are created. This is an optional integration "
        "function, not an automatic assessment or decision-ranking mechanism.",
        input_schema=CALCULATE_INPUT, output_schema=CALCULATE_OUTPUT,
        effects=frozenset({IntegrationEffect.READ}), resource=ResourceSpec("valence.model", ("person_id",)),
        risk=RiskLevel.LOW, presentation=OperationPresentation(
            usage_example={"operation": "valence.utility.calculate", "input": {
                "person_id": "person-id", "actions": [{"id": "candidate", "action": {
                    "title": "Encoded candidate", "duration_minutes": 60, "net_money_cost": 0,
                    "features": dict.fromkeys(NATIVE_INPUT["action"]["properties"]["features"]["required"], 0),
                }}], "state": {"date": "2026-10-16"},
            }}, normalized_errors=ERRORS,
        ),
    ),
)
