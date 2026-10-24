"""Action shapes for pending approvals, independent of how work was discovered."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FunctionStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    function: str = Field(min_length=1)
    arguments: dict[str, Any]

class FunctionsAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["functions"]
    description: str = Field(min_length=1)
    steps: list[FunctionStep] = Field(min_length=1)

class ActAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["act"]
    description: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
