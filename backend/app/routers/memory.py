from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from app.schemas.memory_browser import (
    ChatHistoryDaysPage,
    ChatHistoryPage,
    DailySummaryPage,
)
from app.services.agent_memory_service import AgentMemoryService

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/daily-summaries", response_model=DailySummaryPage)
def list_daily_summaries(
    offset: int = Query(default=0, ge=0, le=1_000_000_000),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict:
    return _memory().list_daily_summaries_page(offset, limit)


@router.get("/chat-history/days", response_model=ChatHistoryDaysPage)
def list_chat_history_days(
    offset: int = Query(default=0, ge=0, le=1_000_000_000),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict:
    return _memory().list_chat_history_days_page(offset, limit)


@router.get("/chat-history", response_model=ChatHistoryPage)
def read_chat_history(
    day: str,
    agent: Literal["assistant", "act", "observer"] | None = Query(default=None),
    offset: int = Query(default=0, ge=0, le=1_000_000_000),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    try:
        return _memory().read_chat_history_page(day, agent, offset, limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _memory() -> AgentMemoryService:
    # Browsing reads canonical files directly. In particular, do not migrate or
    # synchronize database transcripts as a side effect of opening Memory.
    return AgentMemoryService()
