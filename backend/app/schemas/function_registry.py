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


class FunctionCatalogEntryRead(BaseModel):
    id: str
    category: Literal["backend_core", "user", "integration"]
    title: str
    description: str
    risk_level: RiskLevel
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    availability: FunctionAvailability
    availability_reasons: list[str] = Field(default_factory=list)
    invocation: dict[str, Any] = Field(default_factory=dict)
    call_name: str | None = None
    provider: str | None = None
    skill_id: int | None = None
    active_version: str | None = None
    mcp_exposed: bool = True
    mcp_read_only: bool | None = None
    mcp_destructive: bool | None = None
    mcp_open_world: bool | None = None
    mcp_contract_fingerprint: str | None = None


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
