from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApprovalStatus, PermissionRequestScope, RiskLevel


class ApprovalRequestBase(BaseModel):
    skill_id: int | None = None
    generation_request_id: int | None = None
    schedule_id: int | None = None
    request_scope: PermissionRequestScope
    request_type: str = Field(min_length=1, max_length=64)
    risk_level: RiskLevel
    requested_permissions_json: dict[str, Any]
    requested_dependencies_json: list[str] = Field(default_factory=list)
    requested_network_domains_json: list[str] = Field(default_factory=list)
    requested_filesystem_json: dict[str, Any] = Field(default_factory=dict)
    reason_json: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)
    user_explanation: str = ""
    status: ApprovalStatus = "pending"
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    decision_notes: str | None = None


class ApprovalRequestRead(ApprovalRequestBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class ApprovalDecision(BaseModel):
    decision_notes: str | None = None
