"""Typed contracts for native Atlas lifecycle and custom-field operations."""

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ResourceSpec,
    RiskLevel,
)


def _object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_REVISION = {"type": "integer", "minimum": 1}
_JSON_OBJECT = {"type": "object"}
_CURRENT = {"id": _ID, "expected_revision": _REVISION}
_HISTORICAL = {**_CURRENT, "revision": _REVISION}
_FIELD = {
    "category": _ID,
    "name": {"type": "string", "minLength": 1},
    "type": {"enum": ["text", "longText", "number", "boolean", "date", "url", "singleChoice"]},
    "options": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
}
_ERRORS = (
    "atlas_locked", "invalid_input", "not_found", "stale_revision", "provider_unavailable",
    "provider_timeout", "response_too_large", "operation_undeclared", "authorization_missing_or_stale",
)


def _operation(name: str, description: str, inputs: dict, required: tuple[str, ...],
               output_key: str, effect: IntegrationEffect, example: dict,
               risk: RiskLevel = RiskLevel.MEDIUM, output_value: dict | None = None) -> IntegrationOperationSpec:
    operation_id = f"atlas.{name}"
    return IntegrationOperationSpec(
        id=operation_id, title=f"Atlas {name.replace('.', ' ')}", description=description,
        input_schema={**_object(inputs, required), **({"minProperties": 2} if name == "custom_fields.update" else {})},
        output_schema=_object({output_key: _JSON_OBJECT if output_value is None else output_value}, (output_key,)),
        effects=frozenset({effect}), resource=ResourceSpec(f"atlas.{name.split('.')[0]}", ("id",) if "id" in inputs else ()),
        risk=risk,
        presentation=OperationPresentation(
            usage_example={"operation": operation_id, "input": example}, normalized_errors=_ERRORS,
        ),
    )


ATLAS_LIFECYCLE_SPECS = (
    _operation("records.trash", "Move one record to Trash at expected_revision. Reversible with records.restore; "
               "Atlas enforces graph and fixed-resource constraints. Does not permanently purge data.",
               _CURRENT, ("id", "expected_revision"), "record", IntegrationEffect.DELETE,
               {"id": "record-id", "expected_revision": 1}),
    _operation("records.restore", "Restore one trashed record at expected_revision. Atlas validates current invariants.",
               _CURRENT, ("id", "expected_revision"), "record", IntegrationEffect.UPDATE,
               {"id": "record-id", "expected_revision": 2}),
    _operation("records.restore_revision", "Restore a historical record revision as a new current revision. "
               "revision selects history; expected_revision must match the current record. Restores historical trash "
               "status too. confirm_currency_change explicitly acknowledges a populated Cash currency change "
               "without conversion. Atlas revalidates current graph and custom-field constraints; stale edits fail.",
               {**_HISTORICAL, "confirm_currency_change": {"type": "boolean"}},
               ("id", "revision", "expected_revision"), "record", IntegrationEffect.UPDATE,
               {"id": "record-id", "revision": 1, "expected_revision": 3}),
    _operation("trash.empty", "Permanently purge ALL currently trashed records and their retained history and "
               "attachments. Irreversible; inspect records.search with trashed=true first. Atlas validates remaining "
               "model references. Requires per-invocation approval.",
               {}, (), "result", IntegrationEffect.DELETE, {}, RiskLevel.HIGH),
    _operation("links.delete", "Delete one relationship at expected_revision, retaining its revision history. "
               "Recover with links.restore_revision using a non-deleted historical revision and the current revision "
               "from links.get or history.list. Atlas validates graph constraints.",
               _CURRENT, ("id", "expected_revision"), "result", IntegrationEffect.DELETE,
               {"id": "link-id", "expected_revision": 1}),
    _operation("links.restore_revision", "Restore a historical link revision as a new revision, including its "
               "historical deleted status. revision selects history; expected_revision matches the current link "
               "even when deleted. Atlas validates current graph constraints.",
               _HISTORICAL, ("id", "revision", "expected_revision"), "link", IntegrationEffect.UPDATE,
               {"id": "link-id", "revision": 1, "expected_revision": 2}),
    _operation("custom_fields.list", "List complete native custom-field definitions, optionally for one category.",
               {"category": _ID}, (), "fields", IntegrationEffect.READ, {}, RiskLevel.LOW,
               {"type": "array", "items": _JSON_OBJECT}),
    _operation("custom_fields.create", "Create a native typed custom-field definition for one record category. "
               "singleChoice requires options; other types require empty or omitted options. Record values are "
               "written through records.create/update customFieldValues. Atlas validates native field constraints.",
               _FIELD, ("category", "name", "type"), "field", IntegrationEffect.CREATE,
               {"category": "interest", "name": "Context", "type": "text"}),
    _operation("custom_fields.update", "Patch a custom-field definition. category cannot change; type/options "
               "must preserve every current and historical value. Atlas provides no revision guard for definitions. "
               "Archived fields cannot be edited. Read definitions before editing.",
               {"id": _ID, **_FIELD}, ("id",), "field", IntegrationEffect.UPDATE,
               {"id": "field-id", "name": "Updated context"}),
    _operation("custom_fields.delete", "Permanently delete a custom-field definition and irreversibly purge its "
               "values from ALL current records and retained revisions in its category. Requires per-invocation "
               "approval. Atlas provides no revision guard for definitions.",
               {"id": _ID}, ("id",), "result", IntegrationEffect.DELETE, {"id": "field-id"}, RiskLevel.HIGH),
)
ATLAS_LIFECYCLE_OPERATIONS = frozenset(spec.id for spec in ATLAS_LIFECYCLE_SPECS)
