from typing import Literal

from pydantic import BaseModel


class DailySummaryRead(BaseModel):
    day: str
    summary: str
    path: str
    source_turn_id: int | None = None


class DailySummaryPage(BaseModel):
    items: list[DailySummaryRead]
    total: int
    offset: int
    limit: int


class ChatHistoryDaysPage(BaseModel):
    items: list[str]
    total: int
    offset: int
    limit: int


class ChatHistoryMessageRead(BaseModel):
    agent: Literal["assistant", "act", "observer"]
    role: Literal["user", "assistant"]
    text: str
    timestamp: str
    session_id: int
    turn_id: int
    source: Literal["turn", "live"]
    source_id: int


class ChatHistoryPage(BaseModel):
    items: list[ChatHistoryMessageRead]
    total: int
    offset: int
    limit: int
