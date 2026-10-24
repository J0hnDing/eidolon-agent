"""Native Atlas writes through the existing authorization and audit boundary."""

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ResourceSpec,
    RiskLevel,
)

_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_PROVENANCE = {
    "type": "object", "properties": {
        "attribution": {"enum": ["stated", "observed", "inferred", "unknown"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "object"}, "maxItems": 100},
        "scope": {"type": "string"}, "validFrom": {"type": "string"}, "validUntil": {"type": "string"},
    }, "required": ["attribution"], "additionalProperties": False,
}
_RECORD = {
    "category": _ID, "title": {"type": "string", "minLength": 1, "maxLength": 200},
    "data": {"type": "object", "properties": {"provenance": _PROVENANCE}},
    "customFieldValues": {"type": "object"},
}
_LINK = {
    "sourceId": _ID, "targetId": _ID, "type": _ID,
    "label": {"type": "string"}, "notes": {"type": "string"},
    "beliefIds": {"type": "array", "items": _ID}, "provenance": _PROVENANCE,
    "qualifier": {"type": "string"}, "degreePercent": {"type": "number", "minimum": 0, "maximum": 100},
}


def _write(kind: str, action: str) -> IntegrationOperationSpec:
    fields = dict(_RECORD if kind == "records" else _LINK)
    creating = action == "create"
    if creating:
        required = ["category", "title", "data"] if kind == "records" else ["sourceId", "targetId", "type", "provenance"]
        if kind == "records":
            fields["data"] = {**fields["data"], "required": ["provenance"]}
    else:
        fields.pop("category", None)
        if kind == "records":
            fields["confirmCurrencyChange"] = {"type": "boolean"}
        fields.update(id=_ID, expected_revision={"type": "integer", "minimum": 1})
        required = ["id", "expected_revision"]
    name = f"atlas.{kind}.{action}"
    example = ({"category": "interest", "title": "Astronomy", "data": {
        "provenance": {"attribution": "stated", "evidence": []}}}
        if kind == "records" else {"sourceId": "goal-id", "targetId": "end-id", "type": "SERVES",
                                   "provenance": {"attribution": "stated", "evidence": []}})
    if not creating:
        example = {"id": "record-id", "expected_revision": 1,
                   "title" if kind == "records" else "notes": "Updated after user clarification"}
    return IntegrationOperationSpec(
        id=name, title=f"Atlas {kind} {action}",
        description=("Create a native Atlas record/link with explicit provenance." if creating else
                     "Patch a native Atlas record/link at expected_revision. Stale edits fail without retry. "
                     "Supply provenance on personal data changes; retain evidence and distinguish inference.") +
                    " Discover native fields with atlas.describe; Atlas validates category and relationship invariants. "
                    "Field names are native camelCase. " +
                    ("data replaces the complete data object; read first and preserve untouched values. "
                     "Cash currency updates require confirmCurrencyChange=true. " if kind == "records" else "") +
                    "No arbitrary endpoint or secret access.",
        input_schema={"type": "object", "properties": fields, "required": required,
                      "additionalProperties": False, **({} if creating else {"minProperties": 3})},
        output_schema={"type": "object", "properties": {
            "record" if kind == "records" else "link": {"type": "object"}},
            "required": ["record" if kind == "records" else "link"], "additionalProperties": False},
        effects=frozenset({IntegrationEffect.CREATE if creating else IntegrationEffect.UPDATE}),
        resource=ResourceSpec(f"atlas.{kind}", () if creating else ("id",)), risk=RiskLevel.MEDIUM,
        contract_version=2 if kind == "records" and not creating else 1,
        presentation=OperationPresentation(
            usage_example={"operation": name, "input": example},
            normalized_errors=("atlas_locked", "invalid_input", "not_found", "stale_revision",
                               "provider_unavailable", "provider_timeout", "authorization_missing_or_stale"),
        ),
    )


ATLAS_WRITE_SPECS = tuple(_write(kind, action) for kind in ("records", "links") for action in ("create", "update"))
ATLAS_WRITE_OPERATIONS = frozenset(spec.id for spec in ATLAS_WRITE_SPECS)
