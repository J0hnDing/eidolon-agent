from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActSessionCreate(BaseModel):
    origin: Literal["web", "telegram"] = "web"


class ActTurnCreate(BaseModel):
    message: str = Field(min_length=1, max_length=16000)


class ActTurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    codex_turn_id: str | None
    user_message: str
    assistant_message: str | None
    activity_json: list[dict]
    status: str
    error_message: str | None
    cancel_requested_at: datetime | None
    delivery_status: str | None
    created_at: datetime
    completed_at: datetime | None


class ActSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    origin: str
    status: str
    created_at: datetime
    updated_at: datetime
    turns: list[ActTurnRead] = Field(default_factory=list)


class ActSessionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    origin: str
    status: str
    created_at: datetime
    updated_at: datetime
