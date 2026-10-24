"""Native States edit contracts."""

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ResourceSpec,
    RiskLevel,
)

STATE_KEYS = (
    "physical_activity", "mental_stimulation", "social_interaction", "close_connection",
    "goal_progression", "physical_energy", "mental_energy",
)
NEED_KEYS = STATE_KEYS[:5]
_UNIT = {"type": "number", "minimum": 0, "maximum": 1}
_RATE = {"type": "number", "minimum": 0, "maximum": 1000}
_INTENSITY = {"type": "number", "minimum": -1, "maximum": 1}
_EVENT_ID = {"type": "string", "minLength": 1, "maxLength": 200,
             "pattern": r"^[^\s\x00-\x1f\x7f](?:[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f])?$"}
_TIMESTAMP = {"type": "string", "format": "date-time", "pattern": (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$"
)}
_ERRORS = ("atlas_locked", "invalid_input", "stale_revision", "event_id_conflict",
           "provider_unavailable", "provider_timeout", "response_too_large",
           "authorization_missing_or_stale")


def _object(properties: dict, required: tuple = (), **extra) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False, **extra}


def _parameters(key: str) -> dict:
    fields = {"responseRate": _RATE, "confidenceDecayRate": _RATE,
              "confidenceFloor": _UNIT, "confidenceGainRate": _RATE}
    fields.update({"decayRate": _RATE, "optimalLower": _UNIT, "optimalUpper": _UNIT}
                  if key in NEED_KEYS else {"recoveryRate": _RATE, "recoveryBaseline": _UNIT})
    return _object(fields, minProperties=1)


def _targets(value: dict) -> dict:
    return {"type": "object", "propertyNames": _EVENT_ID,
            "additionalProperties": value, "maxProperties": 1000}


_EVIDENCE = _object({
    "attribution": {"enum": ["user_report", "observed", "inferred"]},
    "source": {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"},
    "note": {"type": "string", "minLength": 1, "maxLength": 2000, "pattern": r"\S"},
}, ("attribution", "source", "note"))


def _with_updates(properties: dict, required: tuple, *updates: str) -> dict:
    return _object(properties, required, anyOf=[{
        "required": [key], "properties": {key: {"minItems" if key == "impacts" else "minProperties": 1}},
    } for key in updates])


def _spec(name, title, description, input_schema, output_schema, effect, example):
    return IntegrationOperationSpec(
        id=name, title=title, description=description, input_schema=input_schema,
        output_schema=output_schema, effects=frozenset({effect}), risk=RiskLevel.MEDIUM,
        resource=ResourceSpec("atlas.states"),
        contract_version=3,
        presentation=OperationPresentation(
            usage_example={"operation": name, "input": example}, normalized_errors=_ERRORS,
        ),
    )


_EVENT_OUTPUT = _object({"event": {"type": "object"}, "duplicate": {"type": "boolean"}}, ("event", "duplicate"))
ATLAS_STATE_WRITE_SPECS = (
    _spec(
        "atlas.states.configuration.update", "Update Atlas States configuration",
        "Patch native States or per-record goal, interest and relationship target dynamics prospectively at "
        "expected_revision. Read current States and configuration first to discover target record IDs; "
        "stale edits fail without retry. Rates are per hour or per dose as documented by atlas.describe. "
        "Need optimal bounds must match an imported Valence model when bound. Configure individual targets "
        "for derived summaries: Goals and Interests feed goal_progression; personal Relationships feed "
        "close_connection. Canonical optimal bounds still apply to summaries; their global dynamics govern "
        "fallback only when no relevant live records exist. "
        "History is immutable.",
        _with_updates({"expected_revision": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
                       "states": _object({key: _parameters(key) for key in STATE_KEYS}),
                       "targets": _targets(_parameters("goal_progression"))},
                      ("expected_revision",), "states", "targets"),
        _object({"configuration": {"type": "object"}}, ("configuration",)), IntegrationEffect.UPDATE,
        {"expected_revision": 1, "states": {"physical_energy": {"recoveryRate": 0.2}}},
    ),
    _spec(
        "atlas.states.events.create", "Register Atlas States activity",
        "Register a completed activity with explicit eventId, timestamps, confidence and state impacts or "
        "per-record targets. Target impacts are one-time intensity stimuli, never duration-weighted, and require "
        "evidence distinguishing user_report, observed and inferred attribution. Use targets for specific records; "
        "Goals and Interests feed goal_progression; personal Relationships feed close_connection. Direct "
        "impacts for either summary reject while relevant live records exist; update individual targets instead. "
        "Six states use intensity times durationHours (defaults to full event duration); goal_progression is "
        "one stimulus and forbids durationHours. Events must follow initialization and end no later than now. "
        "Identical eventId retries return duplicate=true; changed reuse fails. Saved history is immutable.",
        _with_updates({"eventId": _EVENT_ID, "startedAt": _TIMESTAMP, "endedAt": _TIMESTAMP, "confidence": _UNIT,
                 "impacts": {"type": "array", "maxItems": 7, "items": {"oneOf": [
                     _object({"state": {"const": "goal_progression", "type": "string"},
                              "intensity": _INTENSITY}, ("state", "intensity")),
                     _object({"state": {"enum": [key for key in STATE_KEYS if key != "goal_progression"]},
                              "intensity": _INTENSITY,
                              "durationHours": {"type": "number", "minimum": 0}}, ("state", "intensity")),
                 ]}},
                 "targets": _targets(_object({"intensity": _INTENSITY, "evidence": _EVIDENCE},
                                              ("intensity", "evidence")))},
                      ("eventId", "startedAt", "endedAt", "confidence"), "impacts", "targets"),
        _EVENT_OUTPUT, IntegrationEffect.CREATE,
        {"eventId": "activity-user-report-1", "startedAt": "2026-10-20T14:00:00Z",
         "endedAt": "2026-10-20T15:00:00Z", "confidence": 0.8,
         "impacts": [{"state": "physical_activity", "intensity": 0.5}]},
    ),
    _spec(
        "atlas.states.observations.create", "Set Atlas States estimates",
        "Set current native States or individual goal, interest and relationship satisfaction estimates and "
        "confidence exactly in [0,1], including zero, using a stable eventId. Target estimates require evidence "
        "distinguishing user_report, observed and inferred attribution. Use targets for specific records; "
        "Goals and Interests feed goal_progression; personal Relationships feed close_connection. Direct "
        "estimates for either summary reject while relevant live records exist; update individual targets instead. "
        "Saves an immutable manual estimate at server now. Identical retries return duplicate=true without "
        "resetting time; changed eventId reuse fails. Earlier history and later dynamics remain effective.",
        _with_updates({"eventId": _EVENT_ID, "states": _object({key: _object(
            {"value": _UNIT, "confidence": _UNIT}, ("value", "confidence")) for key in STATE_KEYS}),
            "targets": _targets(_object({"value": _UNIT, "confidence": _UNIT, "evidence": _EVIDENCE},
                                        ("value", "confidence", "evidence")))},
            ("eventId",), "states", "targets"), _EVENT_OUTPUT, IntegrationEffect.CREATE,
        {"eventId": "manual-user-report-1", "states": {"mental_energy": {"value": 0, "confidence": 0.8}}},
    ),
)
ATLAS_STATE_WRITE_OPERATIONS = frozenset(spec.id for spec in ATLAS_STATE_WRITE_SPECS)
