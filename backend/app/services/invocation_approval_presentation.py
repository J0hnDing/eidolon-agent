from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PRESENTATION_VERSION = 1


@dataclass(frozen=True)
class ApprovalPresentationField:
    label: str
    value: str
    multiline: bool = False
    code: bool = False


@dataclass(frozen=True)
class ApprovalPresentation:
    approval_id: int
    preset: str
    action: str
    caller: str
    fields: tuple[ApprovalPresentationField, ...]
    reason: str


@dataclass(frozen=True)
class ApprovalMessagePresentation:
    heading: str
    lead: str | None
    fields: tuple[ApprovalPresentationField, ...]


_ERROR_MEANINGS = {
    "authorization_missing_or_stale": "The required authorization is missing or no longer matches this action.",
    "connection_changed": "The connected provider account changed before the action could execute.",
    "execution_failed": "The approved action could not be completed.",
    "function_failed": "The approved function ran but did not complete successfully.",
    "function_unavailable": "The function was unavailable when Eidolon attempted to execute it.",
    "interrupted_execution": "Eidolon restarted during execution, so the external outcome cannot be confirmed.",
    "invalid_input": "The provider rejected the action input.",
    "provider_forbidden": "The connected provider refused the requested action.",
    "provider_timeout": "The provider did not complete the action before the request timed out.",
    "provider_unavailable": "The provider was unavailable when Eidolon attempted the action.",
    "rate_limited": "The provider temporarily rejected the action because its rate limit was reached.",
    "stale_contract": "The function or integration contract changed before execution.",
}


def build_approval_presentation(
    *,
    approval_id: int,
    action: str,
    caller: str,
    input_json: Mapping[str, Any],
    reason: str,
) -> ApprovalPresentation:
    if action == "email.send":
        fields = _email_fields(input_json)
        preset = "email_send"
    else:
        fields = tuple(
            ApprovalPresentationField(
                label=_humanize_key(str(key)),
                value=_friendly_value(value),
                multiline=_is_multiline(value),
            )
            for key, value in input_json.items()
        )
        preset = "generic"
    return ApprovalPresentation(
        approval_id=approval_id,
        preset=preset,
        action=action,
        caller=caller,
        fields=fields,
        reason=reason,
    )


def presentation_snapshot(presentation: ApprovalPresentation) -> dict[str, Any]:
    return {
        "version": PRESENTATION_VERSION,
        "preset": presentation.preset,
        "action": presentation.action,
        "caller": presentation.caller,
        "fields": [
            {
                "label": field.label,
                "value": field.value,
                "multiline": field.multiline,
            }
            for field in presentation.fields
        ],
        "reason": presentation.reason,
    }


def presentation_from_snapshot(
    approval_id: int,
    value: Mapping[str, Any],
) -> ApprovalPresentation | None:
    if value.get("version") != PRESENTATION_VERSION:
        return None
    preset = value.get("preset")
    action = value.get("action")
    caller = value.get("caller")
    reason = value.get("reason")
    raw_fields = value.get("fields")
    if (
        preset not in {"email_send", "generic"}
        or not isinstance(action, str)
        or not action
        or not isinstance(caller, str)
        or not caller
        or not isinstance(reason, str)
        or not reason
        or not isinstance(raw_fields, list)
    ):
        return None
    fields: list[ApprovalPresentationField] = []
    for raw_field in raw_fields:
        if not isinstance(raw_field, Mapping):
            return None
        label = raw_field.get("label")
        field_value = raw_field.get("value")
        multiline = raw_field.get("multiline", False)
        if (
            not isinstance(label, str)
            or not label
            or not isinstance(field_value, str)
            or not isinstance(multiline, bool)
        ):
            return None
        fields.append(
            ApprovalPresentationField(label=label, value=field_value, multiline=multiline)
        )
    return ApprovalPresentation(
        approval_id=approval_id,
        preset=preset,
        action=action,
        caller=caller,
        fields=tuple(fields),
        reason=reason,
    )


def initial_message(presentation: ApprovalPresentation) -> ApprovalMessagePresentation:
    return ApprovalMessagePresentation(
        heading="🤔 Approval required",
        lead=None,
        fields=(
            ApprovalPresentationField("Action", presentation.action, code=True),
            ApprovalPresentationField("Caller", presentation.caller, code=True),
            *presentation.fields,
            ApprovalPresentationField("Why", presentation.reason, multiline=True),
        ),
    )


def outcome_message(
    presentation: ApprovalPresentation,
    *,
    decision_status: str,
    execution_status: str,
    error_type: str | None,
    error_message: str | None,
) -> ApprovalMessagePresentation:
    if decision_status == "denied":
        heading = "❌ Denied"
    elif execution_status == "succeeded":
        heading = "✅ Approved · Executed"
    else:
        heading = "⚠️ Approved · Execution failed"

    if presentation.preset == "email_send":
        metadata = tuple(
            field
            for field in presentation.fields
            if field.label in {"To", "Cc", "Bcc", "Subject"}
        )
        lead = "Sent email" if execution_status == "succeeded" else "Email not sent"
    else:
        metadata = (
            ApprovalPresentationField("Action", presentation.action, code=True),
            ApprovalPresentationField("Caller", presentation.caller, code=True),
            *presentation.fields,
            ApprovalPresentationField("Why", presentation.reason, multiline=True),
        )
        lead = None

    error_fields: tuple[ApprovalPresentationField, ...] = ()
    if decision_status == "approved" and execution_status != "succeeded":
        code = error_type or "execution_failed"
        error_fields = (
            ApprovalPresentationField("Error code", code),
            ApprovalPresentationField(
                "Meaning",
                _ERROR_MEANINGS.get(code, "The approved action could not be completed."),
                multiline=True,
            ),
        )
        if error_message:
            error_fields += (
                ApprovalPresentationField("Details", error_message[:2000], multiline=True),
            )

    return ApprovalMessagePresentation(
        heading=heading,
        lead=lead,
        fields=(*metadata, *error_fields),
    )


def _email_fields(input_json: Mapping[str, Any]) -> tuple[ApprovalPresentationField, ...]:
    fields: list[ApprovalPresentationField] = []
    for key, label in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
        value = input_json.get(key)
        if value not in (None, [], ""):
            fields.append(ApprovalPresentationField(label, _friendly_value(value)))
    fields.append(ApprovalPresentationField("Subject", _friendly_value(input_json.get("subject"))))
    fields.append(
        ApprovalPresentationField(
            "Body",
            _friendly_value(input_json.get("body")),
            multiline=True,
        )
    )
    return tuple(fields)


def _humanize_key(value: str) -> str:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value.replace("_", " ").replace("-", " "))
    normalized = " ".join(spaced.split())
    return normalized[:1].upper() + normalized[1:] if normalized else "Input"


def _friendly_value(value: Any, *, depth: int = 0) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        if not value:
            return "None"
        lines: list[str] = []
        for key, item in value.items():
            rendered = _friendly_value(item, depth=depth + 1)
            indent = "  " * depth
            if "\n" in rendered:
                nested = "\n".join(f"{indent}  {line}" for line in rendered.splitlines())
                lines.append(f"{indent}{_humanize_key(str(key))}:\n{nested}")
            else:
                lines.append(f"{indent}{_humanize_key(str(key))}: {rendered}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "None"
        if all(not isinstance(item, (Mapping, list)) for item in value):
            return ", ".join(_friendly_value(item, depth=depth + 1) for item in value)
        return "\n".join(
            f"- {_friendly_value(item, depth=depth + 1)}" for item in value
        )
    return str(value)


def _is_multiline(value: Any) -> bool:
    if isinstance(value, str):
        return "\n" in value
    if isinstance(value, Mapping):
        return True
    if isinstance(value, list):
        return any(
            isinstance(item, (Mapping, list))
            or isinstance(item, str)
            and "\n" in item
            for item in value
        )
    return False
