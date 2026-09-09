from pydantic import BaseModel, ConfigDict, Field


class SkillModelRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: int
    model: str | None = None
    reasoning_effort: str | None = None


class SkillModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)
