from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActSession, ActTurn
from app.services.act_session_service import ActSessionService


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


class _Sessions:
    def __init__(self):
        self.interrupted = []
        self.archived = []

    def interrupt_turn(self, thread_id, turn_id):
        self.interrupted.append((thread_id, turn_id))

    def archive_thread(self, thread_id):
        self.archived.append(thread_id)


def _session(db, thread_id):
    session = ActSession(agent_id="act", codex_thread_id=thread_id)
    db.add(session)
    db.commit()
    return session


def test_running_cancel_uses_only_the_requested_sessions_runtime(db, monkeypatch):
    first = _session(db, "thread-first")
    second = _session(db, "thread-second")
    turns = [
        ActTurn(session_id=session.id, user_message="Work", status="running", codex_turn_id=f"turn-{session.id}")
        for session in (first, second)
    ]
    db.add_all(turns)
    db.commit()
    runtimes = {session.id: SimpleNamespace(sessions=_Sessions()) for session in (first, second)}
    routes = []

    def runtime_for(agent_id, session_id):
        routes.append((agent_id, session_id))
        return runtimes[session_id]

    monkeypatch.setattr("app.services.act_session_service.get_agent_app_server", runtime_for)
    service = ActSessionService(db, telegram_sync=lambda _db, _session: None)

    service.cancel_turn(first.id, turns[0].id)

    assert routes == [("act", first.id)]
    assert runtimes[first.id].sessions.interrupted == [("thread-first", f"turn-{first.id}")]
    assert runtimes[second.id].sessions.interrupted == []
    assert turns[1].cancel_requested_at is None
    assert turns[1].status == "running"


def test_archive_releases_only_the_requested_runtime_and_preserves_identity(db, monkeypatch):
    first = _session(db, "thread-first")
    second = _session(db, "thread-second")
    runtimes = {session.id: SimpleNamespace(sessions=_Sessions()) for session in (first, second)}
    stopped = []
    monkeypatch.setattr("app.services.act_session_service.get_agent_app_server", lambda _agent_id, session_id: runtimes[session_id])
    monkeypatch.setattr("app.services.act_session_service.stop_act_app_server", stopped.append)
    first_id = first.id

    ActSessionService(db).archive(first.id)

    assert stopped == [first_id]
    assert runtimes[first_id].sessions.archived == ["thread-first"]
    assert runtimes[second.id].sessions.archived == []
    assert first.id == first_id
    assert first.codex_thread_id == "thread-first"
    assert first.status == "archived"
    assert second.status == "active"


def test_read_list_and_create_do_not_allocate_an_act_runtime(db, monkeypatch):
    def unexpected_runtime(*_args):
        pytest.fail("Session metadata operations must not allocate a runtime")

    monkeypatch.setattr("app.services.act_session_service.get_agent_app_server", unexpected_runtime)
    service = ActSessionService(db, telegram_sync=lambda _db, _session: None)

    session = service.create_session()

    assert service.read_session(session.id).id == session.id
    assert [item.id for item in service.list_sessions()] == [session.id]
