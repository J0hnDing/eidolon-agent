from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import RiskLevel, SkillStatus


class SkillBase(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    status: SkillStatus = "proposed"
    risk_level: RiskLevel = "low"
    manifest_path: str = Field(min_length=1, max_length=512)
    installed_path: str | None = Field(default=None, max_length=512)
    enabled: bool = False


class SkillCreate(SkillBase):
    pass


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    description: str | None = Field(default=None, min_length=1)
    status: SkillStatus | None = None
    risk_level: RiskLevel | None = None
    manifest_path: str | None = Field(default=None, min_length=1, max_length=512)
    installed_path: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class SkillRead(SkillBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
