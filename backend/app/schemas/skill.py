from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import RiskLevel, SkillRuntime, SkillStatus


class SkillBase(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1)
    runtime: SkillRuntime = "function"
    status: SkillStatus = "proposed"
    risk_level: RiskLevel = "low"
    manifest_path: str = Field(min_length=1, max_length=512)
    instructions_path: str | None = Field(default=None, max_length=512)
    input_schema_json: dict[str, Any] | None = None
    output_schema_json: dict[str, Any] | None = None
    function_requirements_json: list[str] = Field(default_factory=list)
    integration_requirements_json: list[dict[str, Any]] = Field(default_factory=list)
    installed_path: str | None = Field(default=None, max_length=512)
    active_version_id: int | None = None
    enabled: bool = False


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None


class SkillRead(SkillBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_running: bool = False
    created_at: datetime
    updated_at: datetime
