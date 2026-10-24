from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActSessionCreate(BaseModel):
    origin: Literal["web", "telegram", "wecom"] = "web"


class ActTurnCreate(BaseModel):
    message: str = Field(min_length=1, max_length=16000)


class ActLiveMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: Literal["assistant", "user"]
    text: str
    reply_to_message_id: int | None
    reply_status: str
    reply_expires_at: datetime | None
    created_at: datetime


class ActTurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    codex_turn_id: str | None
    user_message: str
    assistant_message: str | None
    backend_message_kind: str | None = None
    assistant_context_json: dict | None = None
    activity_json: list[dict]
    live_messages: list[ActLiveMessageRead] = Field(default_factory=list)
    status: str
    error_message: str | None
    cancel_requested_at: datetime | None
    delivery_provider: str | None
    delivery_message_thread_id: int | None
    delivery_status: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ActSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: str = "act"
    title: str
    origin: str
    status: str
    created_at: datetime
    updated_at: datetime
    wecom_user_id: str | None = None
    turns: list[ActTurnRead] = Field(default_factory=list)


class ActSessionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: str = "act"
    title: str
    origin: str
    status: str
    created_at: datetime
    updated_at: datetime
    wecom_user_id: str | None = None
