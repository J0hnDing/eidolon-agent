from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_risk: Literal["low", "medium", "high"] = "high"
    read_only: bool = True
    allowed_functions: list[str] = Field(default_factory=list, max_length=500)
    banned_functions: list[str] = Field(default_factory=list, max_length=500)
    model: str | None = Field(default=None, max_length=128)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None
