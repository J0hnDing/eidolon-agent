from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

InvocationDecisionStatus = Literal["pending", "approved", "denied"]
InvocationExecutionStatus = Literal[
    "not_started",
    "executing",
    "succeeded",
    "failed",
    "stale",
    "outcome_unknown",
]


class PendingApprovalReceipt(BaseModel):
    status: Literal["pending_approval"] = "pending_approval"
    approval_id: int = Field(ge=1)


class InvocationApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_kind: str
    target_id: str
    target_skill_id: int | None
    target_version_id: int | None
    target_contract_fingerprint: str
    target_description: str
    provider: str | None
    provider_account_id: str | None
    caller_type: str
    source: str
    caller_skill_id: int | None
    caller_version_id: int | None
    caller_run_id: int | None
    web_app_instance_id: str | None
    initiating_action: str | None
    input_json: dict[str, Any]
    input_hash: str
    reason_to_call: str
    presentation_json: dict[str, Any]
    dispatch_metadata_json: dict[str, Any]
    decision_status: InvocationDecisionStatus
    execution_status: InvocationExecutionStatus
    decided_via: str | None
    decided_by: str | None
    telegram_delivery_status: str
    telegram_message_ids_json: list[int]
    result_json: dict[str, Any] | None
    error_type: str | None
    error_message: str | None
    created_at: datetime
    delivered_at: datetime | None
    decided_at: datetime | None
    execution_started_at: datetime | None
    execution_completed_at: datetime | None
    updated_at: datetime


class InvocationApprovalListRead(BaseModel):
    items: list[InvocationApprovalRead]


class InvocationApprovalDecisionRequest(BaseModel):
    decided_by: str = Field(default="local_user", min_length=1, max_length=128)
