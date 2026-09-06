from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class InvocationChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=32)


class ProductManagerRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: InvocationChoice = Field(default_factory=InvocationChoice)
    blueprint_and_permissions: InvocationChoice = Field(default_factory=InvocationChoice)
    task_dag: InvocationChoice = Field(default_factory=InvocationChoice)
    repair: InvocationChoice = Field(default_factory=InvocationChoice)
    update: InvocationChoice = Field(default_factory=InvocationChoice)


class BuilderRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: InvocationChoice = Field(default_factory=InvocationChoice)
    single_codex: InvocationChoice = Field(default_factory=InvocationChoice)
    easy: InvocationChoice = Field(default_factory=InvocationChoice)
    medium: InvocationChoice = Field(default_factory=InvocationChoice)
    hard: InvocationChoice = Field(default_factory=InvocationChoice)
    repair: InvocationChoice = Field(default_factory=InvocationChoice)
    update: InvocationChoice = Field(default_factory=InvocationChoice)


class TesterRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: InvocationChoice = Field(default_factory=InvocationChoice)
    task: InvocationChoice = Field(default_factory=InvocationChoice)
    final_e2e: InvocationChoice = Field(default_factory=InvocationChoice)
    update: InvocationChoice = Field(default_factory=InvocationChoice)


class CodexRoutingSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_build_workflow_override: Literal["single_codex", "task_dag"] | None = None
    act: InvocationChoice = Field(default_factory=InvocationChoice)
    observer: InvocationChoice = Field(default_factory=InvocationChoice)
    assessment: InvocationChoice = Field(default_factory=InvocationChoice)
    product_manager: ProductManagerRouting = Field(default_factory=ProductManagerRouting)
    builder: BuilderRouting = Field(default_factory=BuilderRouting)
    tester: TesterRouting = Field(default_factory=TesterRouting)


class CodexRoutingSettingsRead(CodexRoutingSettingsPayload):
    updated_at: datetime | None = None


class CodexModelOption(BaseModel):
    id: str
    model: str
    display_name: str
    description: str
    is_default: bool
    default_reasoning_effort: str
    supported_reasoning_efforts: list[str]


class CodexModelCatalogRead(BaseModel):
    available: bool
    fetched_at: str
    error: str | None = None
    models: list[CodexModelOption] = Field(default_factory=list)


class ResolvedInvocationSettings(BaseModel):
    provider: str = "codex_cli"
    role: str
    action: str
    difficulty: str | None = None
    route_source: str
    requested_model: str | None = None
    effective_model: str | None = None
    requested_reasoning_effort: str | None = None
    effective_reasoning_effort: str | None = None
