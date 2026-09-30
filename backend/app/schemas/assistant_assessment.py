"""Strict first-turn Assistant assessment contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FunctionStep(StrictModel):
    function: str = Field(min_length=1)
    arguments: dict[str, Any]


class FunctionsAction(StrictModel):
    type: Literal["functions"]
    description: str = Field(min_length=1)
    steps: list[FunctionStep] = Field(min_length=1)


class ActAction(StrictModel):
    type: Literal["act"]
    description: str = Field(min_length=1)
    instruction: str = Field(min_length=1)


Action = FunctionsAction | ActAction


class GoalTarget(StrictModel):
    type: Literal["goal", "subgoal"]
    id: str = Field(min_length=1)


class AbsoluteTiming(StrictModel):
    type: Literal["absolute"]
    at: datetime

    @model_validator(mode="after")
    def require_timezone(self):
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("Absolute follow-up time requires a timezone")
        return self


class CalendarTiming(StrictModel):
    type: Literal["after_calendar_step"]
    step_index: int = Field(ge=0)
    delay_minutes: int = Field(ge=0)


class FollowUp(StrictModel):
    message: str = Field(min_length=1)
    timing: AbsoluteTiming | CalendarTiming


class TodoProposal(StrictModel):
    todo_id: str = Field(min_length=1)
    action: Action


class GoalQuestion(StrictModel):
    target: GoalTarget
    question: str = Field(min_length=1)


class GoalProposal(StrictModel):
    target: GoalTarget
    reason: str = Field(min_length=1)
    action: Action
    follow_up: FollowUp | None = None


class OpportunityProposal(StrictModel):
    action: Action
    follow_up: FollowUp | None = None


class Opportunity(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=20_000)
    proposals: list[OpportunityProposal]


class TodoDoer(StrictModel):
    proposals: list[TodoProposal]


class GoalDoer(StrictModel):
    questions: list[GoalQuestion]
    proposals: list[GoalProposal]


class OpportunityScout(StrictModel):
    opportunities: list[Opportunity] = Field(max_length=100)


class AssistantAssessmentResult(StrictModel):
    todo_doer: TodoDoer
    goal_doer: GoalDoer
    opportunity_scout: OpportunityScout


ASSESSMENT_RESPONSE_SCHEMA = AssistantAssessmentResult.model_json_schema()
