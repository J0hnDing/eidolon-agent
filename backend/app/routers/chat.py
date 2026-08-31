from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MemoryFact, Message
from app.schemas.skill_generation import ChatRequest, ChatResponse
from app.services.agent_workflow_service import AgentWorkflowError
from app.services.chat_orchestrator import ChatOrchestrator

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    try:
        return ChatOrchestrator(db).handle_message(
            payload.message,
            generation_request_id=payload.generation_request_id,
            conversation_id=payload.conversation_id,
        )
    except AgentWorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/chat/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_conversation(conversation_id: str, db: Session = Depends(get_db)) -> Response:
    if len(conversation_id) > 128:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Conversation id is too long")

    message_ids = [
        message_id
        for (message_id,) in db.query(Message.id)
        .filter(Message.conversation_id == conversation_id)
        .all()
    ]
    if message_ids:
        db.query(MemoryFact).filter(MemoryFact.source_message_id.in_(message_ids)).update(
            {"source_message_id": None},
            synchronize_session=False,
        )
        db.query(Message).filter(Message.id.in_(message_ids)).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
