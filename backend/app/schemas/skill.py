from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import InterfaceType, RiskLevel, SkillStatus


class SkillBase(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1)
    interface_type: InterfaceType = "chat"
    status: SkillStatus = "proposed"
    risk_level: RiskLevel = "low"
    manifest_path: str = Field(min_length=1, max_length=512)
    instructions_path: str | None = Field(default=None, max_length=512)
    input_schema_json: dict[str, Any] | None = None
    output_schema_json: dict[str, Any] | None = None
    tool_ui_schema_json: dict[str, Any] | None = None
    installed_path: str | None = Field(default=None, max_length=512)
    active_version_id: int | None = None
    enabled: bool = False


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None


class SkillRead(SkillBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
