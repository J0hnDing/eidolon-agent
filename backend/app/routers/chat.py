from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.skill_generation import ChatRequest, ChatResponse
from app.services.chat_orchestrator import ChatOrchestrator


router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    return ChatOrchestrator(db).handle_message(
        payload.message,
        payload.mode,
        generation_request_id=payload.generation_request_id,
        conversation_id=payload.conversation_id,
    )
