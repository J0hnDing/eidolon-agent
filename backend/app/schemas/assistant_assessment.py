"""Strict first-turn Assistant assessment contract."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_chat import SESSION_TITLE_SCHEMA


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
ASSESSMENT_RESPONSE_SCHEMA["properties"]["title"] = SESSION_TITLE_SCHEMA
ASSESSMENT_RESPONSE_SCHEMA["required"].append("title")
# Strict model output cannot contain an open-ended object. Function arguments are
# variable per catalog entry, so encode them at the transport boundary and keep
# the validated assessment and stored proposals as ordinary JSON objects.
ASSESSMENT_RESPONSE_SCHEMA["$defs"]["FunctionStep"]["properties"]["arguments"] = {
    "type": "string",
    "description": "Function arguments encoded as a JSON object string matching the function input schema.",
}
for _proposal in ("GoalProposal", "OpportunityProposal"):
    _definition = ASSESSMENT_RESPONSE_SCHEMA["$defs"][_proposal]
    _definition["required"].append("follow_up")
    _definition["properties"]["follow_up"].pop("default", None)


def parse_assessment_response(output_text: str) -> AssistantAssessmentResult:
    payload = json.loads(output_text)
    if not isinstance(payload, dict):
        raise ValueError("Assistant assessment response must be an object")
    # Session title metadata is handled by the shared chat dispatcher, not by
    # the assessment proposal/result contract. Older saved responses omit it.
    payload.pop("title", None)
    proposals = list(payload.get("todo_doer", {}).get("proposals", []))
    proposals.extend(payload.get("goal_doer", {}).get("proposals", []))
    for opportunity in payload.get("opportunity_scout", {}).get("opportunities", []):
        proposals.extend(opportunity.get("proposals", []))
    for proposal in proposals:
        action = proposal.get("action", {})
        if action.get("type") != "functions":
            continue
        for step in action.get("steps", []):
            encoded = step.get("arguments")
            if not isinstance(encoded, str):
                raise ValueError("Function arguments must be encoded as a JSON object string")
            try:
                arguments = json.loads(encoded)
            except json.JSONDecodeError as exc:
                raise ValueError("Function arguments must contain valid JSON") from exc
            if not isinstance(arguments, dict):
                raise ValueError("Function arguments must decode to a JSON object")
            step["arguments"] = arguments
    return AssistantAssessmentResult.model_validate(payload)
