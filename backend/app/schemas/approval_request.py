from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ApprovalStatus, RiskLevel


class ApprovalRequestBase(BaseModel):
    skill_id: int
    request_type: str = Field(min_length=1, max_length=64)
    risk_level: RiskLevel
    requested_permissions_json: dict[str, Any]
    reason: str = Field(min_length=1)
    status: ApprovalStatus = "pending"
    resolved_at: datetime | None = None


class ApprovalRequestCreate(ApprovalRequestBase):
    pass


class ApprovalRequestRead(ApprovalRequestBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
