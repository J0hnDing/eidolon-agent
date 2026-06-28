from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillVersionBase(BaseModel):
    skill_id: int
    version: str = Field(min_length=1, max_length=64)
    manifest_json: dict[str, Any]
    code_snapshot_path: str = Field(min_length=1, max_length=512)
    change_summary: str | None = None


class SkillVersionCreate(SkillVersionBase):
    pass


class SkillVersionRead(SkillVersionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
