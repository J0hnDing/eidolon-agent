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


class SkillRunCreate(SkillRunBase):
    pass


class SkillRunRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class SkillRunRead(SkillRunBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
