from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

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


class SkillRunCreate(SkillRunBase):
    pass


class SkillRunRead(SkillRunBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
