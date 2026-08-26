from typing import Literal

from pydantic import BaseModel, Field


class CodexMcpWrite(BaseModel):
    action: Literal["install", "repair"] = "install"


class CodexMcpStatus(BaseModel):
    enabled: bool
    registered: bool
    config_matches: bool
    available_tool_count: int = Field(ge=0)
    excluded_ids: list[str] = Field(default_factory=list)
    config_path: str
    restart_required: bool
    error_type: str | None = None
    error: str | None = None
