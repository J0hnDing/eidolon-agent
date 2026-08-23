from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.approval_request import ApprovalRequestRead
from app.schemas.common import SkillVersionActor, SkillVersionStatus


class SkillVersionBase(BaseModel):
    skill_id: int
    version: str = Field(min_length=1, max_length=64)
    status: SkillVersionStatus = "draft"
    folder_path: str = Field(min_length=1, max_length=512)
    manifest_json: dict[str, Any]
    code_snapshot_path: str = Field(min_length=1, max_length=512)
    created_by: SkillVersionActor = "system"
    parent_version_id: int | None = None
    permission_fingerprint: str = ""
    test_status: str = "not_run"
    validation_status: str = "not_run"
    change_summary: str | None = None
    changelog: str | None = None


class SkillVersionRead(SkillVersionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    activated_at: datetime | None


class SkillUpdateSuggestion(BaseModel):
    suggestion: str = Field(min_length=3, max_length=4000)


class SkillUpdateResponse(BaseModel):
    agent_run_id: int
    version: SkillVersionRead | None = None
    permission_request: ApprovalRequestRead | None = None
    status: str
    message: str


class SkillVersionComparison(BaseModel):
    active_version: SkillVersionRead
    candidate_version: SkillVersionRead
    files: list[dict[str, str | None]]
