from pydantic import BaseModel, Field

from app.schemas.common import SkillType


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
