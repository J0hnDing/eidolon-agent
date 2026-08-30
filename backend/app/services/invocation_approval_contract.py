from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

INVOCATION_APPROVAL_DESCRIPTION_SUFFIX = (
    "Requires per-call approval. Calling this function sends an approval request to Telegram; "
    "the action executes only after user approval. Approval and execution are managed entirely "
    "by Eidolon's backend."
)
REASON_TO_CALL_FIELD = "reason_to_call"
MAX_REASON_TO_CALL_LENGTH = 500
MAX_INVOCATION_APPROVAL_INPUT_BYTES = 32 * 1024


class InvocationApprovalContractError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveInvocationContract:
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    requires_invocation_approval: bool


def effective_invocation_contract(
    *,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    requires_invocation_approval: bool,
) -> EffectiveInvocationContract:
    if not requires_invocation_approval:
        return EffectiveInvocationContract(
            description=description,
            input_schema=deepcopy(input_schema),
            output_schema=deepcopy(output_schema),
            requires_invocation_approval=False,
        )
    properties = input_schema.get("properties")
    if isinstance(properties, dict) and REASON_TO_CALL_FIELD in properties:
        raise InvocationApprovalContractError(
            f"{REASON_TO_CALL_FIELD} is reserved for backend-managed invocation approval"
        )
    projected_input = deepcopy(input_schema)
    projected_properties = projected_input.get("properties")
    if not isinstance(projected_properties, dict):
        projected_properties = {}
        projected_input["properties"] = projected_properties
    projected_properties[REASON_TO_CALL_FIELD] = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_REASON_TO_CALL_LENGTH,
        "description": "Why this action should be executed after the user approves it.",
    }
    required = projected_input.get("required")
    projected_required = list(required) if isinstance(required, list) else []
    if REASON_TO_CALL_FIELD not in projected_required:
        projected_required.append(REASON_TO_CALL_FIELD)
    projected_input["required"] = projected_required
    return EffectiveInvocationContract(
        description=f"{description.rstrip()} {INVOCATION_APPROVAL_DESCRIPTION_SUFFIX}",
        input_schema=projected_input,
        output_schema=pending_approval_output_schema(),
        requires_invocation_approval=True,
    )


def pending_approval_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "const": "pending_approval"},
            "approval_id": {"type": "integer", "minimum": 1},
        },
        "required": ["status", "approval_id"],
        "additionalProperties": False,
    }


def pending_approval_receipt(approval_id: int) -> dict[str, Any]:
    return {"status": "pending_approval", "approval_id": approval_id}


def split_approval_input(input_json: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    reason = input_json.get(REASON_TO_CALL_FIELD)
    if not isinstance(reason, str) or not reason.strip():
        raise InvocationApprovalContractError("reason_to_call must be a non-empty string")
    normalized_reason = reason.strip()
    if len(normalized_reason) > MAX_REASON_TO_CALL_LENGTH:
        raise InvocationApprovalContractError(
            f"reason_to_call must contain at most {MAX_REASON_TO_CALL_LENGTH} characters"
        )
    business_input = dict(input_json)
    business_input.pop(REASON_TO_CALL_FIELD, None)
    return normalized_reason, business_input
