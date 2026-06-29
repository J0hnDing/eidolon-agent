from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import SkillType


class ProposedSampleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    skill_type: SkillType


class SkillFileRead(BaseModel):
    path: str
    content: str


class ProposedSkillValidationRead(BaseModel):
    ok: bool
    skill_type: SkillType | None = None
    manifest_valid: bool = False
    tests_run: bool = False
    tests_passed: bool | None = None
    stdout: str = ""
    stderr: str = ""
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)


ValidationStage = Literal["manifest", "files", "tests", "install"]
