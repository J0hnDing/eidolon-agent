"""Read-only API projections of canonical completed-message files.

Never attach archived text to SQLAlchemy entities: a later unrelated commit
could otherwise turn a display read into a second persisted transcript.
"""

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import ActSession, ActTurn
from app.schemas.act import ActLiveMessageRead, ActSessionRead, ActTurnRead
from app.services.act_chat_message_service import attach_live_messages
from app.services.agent_memory_service import AgentMemoryService


def project_turn(db: Session, turn: ActTurn, *, agent_id: str | None = None, content: dict | None = None) -> ActTurnRead:
    attach_live_messages(db, [turn])
    content = content if content is not None else AgentMemoryService(db).read_turn_content(turn, agent_id=agent_id)
    result = ActTurnRead.model_validate(turn)
    live = [
        ActLiveMessageRead.model_validate(message).model_copy(update={"text": text})
        for message in turn.live_messages
        if (text := content["live_messages"].get(message.id))
    ]
    return result.model_copy(update={
        "user_message": content["user_message"],
        "assistant_message": content["assistant_message"],
        "live_messages": live,
    })


def project_session(db: Session, session: ActSession) -> ActSessionRead:
    memory = AgentMemoryService(db)
    # Also enforce retention when opening an otherwise idle application.
    memory.sync_chat_history()
    db.refresh(session)
    turns = []
    for turn in session.turns:
        content = memory.read_turn_content(turn, agent_id=session.agent_id)
        if turn.history_archived_at is not None and not content["available"]:
            continue
        turns.append(project_turn(db, turn, agent_id=session.agent_id, content=content))
    return ActSessionRead.model_validate(session).model_copy(update={"turns": turns})


def reclaim_history_database_space(engine: Engine) -> None:
    """Reclaim freed migration pages before background workers start.

    Normal operation reuses SQLite's freed pages. VACUUM is confined to startup,
    outside transactions, so it cannot interrupt an active agent turn.
    """
    if engine.dialect.name != "sqlite":
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if connection.exec_driver_sql("PRAGMA freelist_count").scalar_one() > 0:
            connection.exec_driver_sql("VACUUM")
