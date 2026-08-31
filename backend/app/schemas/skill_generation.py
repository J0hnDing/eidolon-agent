from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.agent_run import AgentRunRead
from app.schemas.approval_request import ApprovalRequestRead
from app.schemas.common import GenerationRequestStatus, RiskLevel
from app.schemas.proposed_skill import ProposedSkillValidationRead
from app.schemas.skill import SkillRead


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    generation_request_id: int | None = None
    conversation_id: str | None = None


class SkillGenerationRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_message: str
    proposed_skill_name: str
    proposed_display_name: str
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


class SkillGenerationPlanResponse(BaseModel):
    type: Literal["skill_generation_plan"]
    generation_request: SkillGenerationRequestRead
    permission_request: ApprovalRequestRead


class ProjectNotPlausibleResponse(BaseModel):
    type: Literal["project_not_plausible"]
    message: str
    reason: str


class ProjectNeedsInputResponse(BaseModel):
    type: Literal["project_needs_input"]
    message: str
    question: str
    generation_request: SkillGenerationRequestRead
    agent_run: AgentRunRead


class SkillGenerationApprovalResponse(BaseModel):
    generation_request: SkillGenerationRequestRead
    permission_request: ApprovalRequestRead | None = None
    proposed_skill: SkillRead | None = None
    validation: ProposedSkillValidationRead | None = None
    agent_run: AgentRunRead | None = None
    runtime_permission_request: ApprovalRequestRead | None = None


class ProjectConversationStateRead(BaseModel):
    generation_request: SkillGenerationRequestRead
    permission_request: ApprovalRequestRead | None = None
    proposed_skill: SkillRead | None = None
    agent_run: AgentRunRead | None = None
    runtime_permission_request: ApprovalRequestRead | None = None
    needs_polling: bool = False


ChatResponse = (
    SkillGenerationPlanResponse
    | ProjectNotPlausibleResponse
    | ProjectNeedsInputResponse
)
