from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_risk: Literal["low", "medium", "high"] = "high"
    read_only: bool = True
    allowed_functions: list[str] = Field(default_factory=list, max_length=500)
    banned_functions: list[str] = Field(default_factory=list, max_length=500)
    model: str | None = Field(default=None, max_length=128)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None


class AgentPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=4000)
    instruction: str = Field(min_length=1, max_length=16000)
    actions: str = Field(min_length=1, max_length=4000)
    references: list[str] = Field(
        default_factory=list, max_length=50,
        description="Attach todos as todo:<Notion page ID> and goals as goal:<Atlas goal ID>. "
        "Proposals are removed from history when any attached item is missing or completed.",
    )
    replaces_proposal_id: int | None = Field(default=None, gt=0)
    material_change: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def require_change(self):
        if (self.replaces_proposal_id is None) != (self.material_change is None):
            raise ValueError("A replacement requires both prior proposal ID and material change")
        if any(not reference.strip() or len(reference) > 500 for reference in self.references):
            raise ValueError("References must contain 1-500 characters")
        return self
