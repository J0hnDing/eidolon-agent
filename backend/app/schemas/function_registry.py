from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import RiskLevel
from app.schemas.skill_run import SkillRunRead

FunctionAvailability = Literal["available", "disabled", "unavailable"]
FunctionAccessState = Literal[
    "not_requested",
    "not_declared",
    "no_approval_required",
    "pending",
    "approved",
    "denied",
    "stale",
    "unavailable",
]


class FunctionContractRead(BaseModel):
    skill_id: int
    name: str
    description: str
    active_version_id: int | None
    active_version: str | None
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None
    risk_level: RiskLevel
    permissions: dict[str, Any]
    availability: FunctionAvailability
    availability_reasons: list[str] = Field(default_factory=list)
    declared_by_caller: bool | None = None
    access_state: FunctionAccessState = "not_requested"


class FunctionRequirementReview(BaseModel):
    name: str
    reason: str
    target_skill_id: int | None = None
    description: str | None = None
    risk_level: RiskLevel | None = None
    availability: FunctionAvailability = "unavailable"
    availability_reasons: list[str] = Field(default_factory=list)
    approval_required: bool = False
    access_state: FunctionAccessState = "unavailable"
    approval_request_id: int | None = None


class FunctionInvocationRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class FunctionInvocationResponse(BaseModel):
    run: SkillRunRead
    output: dict[str, Any] | None = None
    error: str | None = None
