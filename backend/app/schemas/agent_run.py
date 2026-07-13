from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import AgentRunStatus, AgentRunStepStatus, AgentRunType, AgentStepName


class AgentRunStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_run_id: int
    step_name: AgentStepName
    task_node_id: str | None
    status: AgentRunStepStatus
    input_json: dict[str, Any] | None
    output_json: dict[str, Any] | None
    logs: str | None
    started_at: datetime | None
    ended_at: datetime | None
    error_message: str | None
    codex_invocations_json: list[dict[str, Any]]
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


class AgentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_type: AgentRunType
    status: AgentRunStatus
    skill_id: int | None
    generation_request_id: int | None
    user_request: str
    summary: str | None
    current_task_id: str | None
    current_step: str | None
    failure_count_json: dict[str, Any]
    build_workflow: str | None
    blueprint_json: dict[str, Any] | None
    final_summary_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    error_message: str | None
    total_input_tokens: int
    total_cached_input_tokens: int
    total_output_tokens: int
    total_reasoning_output_tokens: int
    total_tokens: int
    pause_reason: str | None


class AgentRunDetailRead(AgentRunRead):
    steps: list[AgentRunStepRead] = Field(default_factory=list)


class AgentRunCreateResponse(BaseModel):
    agent_run: AgentRunRead
