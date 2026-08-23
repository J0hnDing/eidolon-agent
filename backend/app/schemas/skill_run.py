from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import SkillRunStatus


class SkillRunBase(BaseModel):
    skill_id: int
    status: SkillRunStatus = "pending"
    input_json: dict[str, Any] | None = None
    output_json: dict[str, Any] | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error_message: str | None = None
    codex_invocations_json: list[dict[str, Any]] = Field(default_factory=list)
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    version_id: int | None = None
    invocation_source: str = "internal"
    caller_skill_id: int | None = None
    caller_version_id: int | None = None
    source_schedule_id: int | None = None
    web_app_instance_id: str | None = None
    initiating_action: str | None = None


class SkillRunRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class SkillRunRead(SkillRunBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
