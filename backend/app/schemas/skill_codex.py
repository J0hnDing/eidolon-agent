from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillCodexPermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_response: bool = True
    internet_access: bool = False


class SkillCodexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    response_schema: dict[str, Any] | None = None
    codex_permissions: SkillCodexPermissionsRequest = Field(default_factory=SkillCodexPermissionsRequest)


class SkillCodexResponse(BaseModel):
    response: str
    model: str | None
    internet_access: bool
