from pydantic import BaseModel

from app.schemas.skill import SkillRead
from app.schemas.skill_run import SkillRunRead


class ToolRead(BaseModel):
    skill: SkillRead
    runtime_permission_status: str
    runtime_blocked_reason: str | None = None


class ToolRunResponse(BaseModel):
    skill: SkillRead
    run: SkillRunRead
