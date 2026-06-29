from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.common import GenerationRequestStatus, RiskLevel, SkillType
from app.schemas.skill import SkillRead
from app.schemas.proposed_skill import ProposedSkillValidationRead
from app.schemas.approval_request import ApprovalRequestRead


class ChatRequest(BaseModel):
    message: str
    mode: Literal["chat", "project"] = "chat"


class SkillGenerationRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_message: str
    proposed_skill_name: str
    proposed_display_name: str
    proposed_skill_type: SkillType
    plan_json: dict[str, Any]
    requested_permissions_json: dict[str, Any]
    requested_dependencies_json: list[str]
    requested_network_domains_json: list[str]
    risk_level: RiskLevel
    status: GenerationRequestStatus
    proposed_skill_id: int | None
    created_at: datetime
    updated_at: datetime
    error_message: str | None


class DirectChatResponse(BaseModel):
    type: Literal["direct_answer"]
    message: str


class SkillGenerationPlanResponse(BaseModel):
    type: Literal["skill_generation_plan"]
    generation_request: SkillGenerationRequestRead
    permission_request: ApprovalRequestRead


class UnsafeChatResponse(BaseModel):
    type: Literal["unsafe_or_unsupported"]
    message: str


class ProjectNotPlausibleResponse(BaseModel):
    type: Literal["project_not_plausible"]
    message: str
    reason: str
    optional_projects: list[str]


class SkillGenerationApprovalResponse(BaseModel):
    generation_request: SkillGenerationRequestRead
    permission_request: ApprovalRequestRead | None = None
    proposed_skill: SkillRead | None = None
    validation: ProposedSkillValidationRead | None = None


ChatResponse = DirectChatResponse | SkillGenerationPlanResponse | UnsafeChatResponse | ProjectNotPlausibleResponse
