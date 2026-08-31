from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ActSession, ActTurn
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.act_turn_dispatcher import ActTurnDispatcher


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)


class FakeThreadSessions:
    def __init__(self) -> None:
        self.archived: list[str] = []
        self.interrupted: list[tuple[str, str]] = []

    def archive_thread(self, thread_id: str) -> None:
        self.archived.append(thread_id)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupted.append((thread_id, turn_id))


class FakeAppServer:
    def __init__(self) -> None:
        self.sessions = FakeThreadSessions()

    def start_thread(self, _db, *, model: str | None, reasoning_effort: str | None) -> str:
        assert model == "gpt-act"
        assert reasoning_effort == "high"
        return "thread-act"


def test_session_enqueue_duplicate_cancel_and_archive(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
) -> None:
    monkeypatch.setattr(
        "app.services.act_session_service.CodexRoutingService.resolve",
        lambda *_args, **_kwargs: SimpleNamespace(
            effective_model="gpt-act",
            effective_reasoning_effort="high",
        ),
    )
    app_server = FakeAppServer()
    db: Session = session_factory()
    try:
        service = ActSessionService(db, app_server=app_server)  # type: ignore[arg-type]
        session = service.create_session()
        turn = service.enqueue_turn(session.id, "Write a report")
        assert turn.status == "queued"
        assert service.read_session(session.id).title == "Write a report"
        with pytest.raises(ActSessionError, match="already queued or running"):
            service.enqueue_turn(session.id, "Second request")
        cancelled = service.cancel_turn(session.id, turn.id)
        assert cancelled.status == "cancelled"
        service.archive(session.id)
        assert app_server.sessions.archived == ["thread-act"]
    finally:
        db.close()


def test_running_turn_cancellation_interrupts_the_live_codex_turn(session_factory) -> None:
    db: Session = session_factory()
    app_server = FakeAppServer()
    try:
        session = ActSession(codex_thread_id="thread-act")
        db.add(session)
        db.flush()
        turn = ActTurn(
            session_id=session.id,
            user_message="long work",
            status="running",
            codex_turn_id="turn-live",
        )
        db.add(turn)
        db.commit()
        service = ActSessionService(db, app_server=app_server)  # type: ignore[arg-type]

        cancelled = service.cancel_turn(session.id, turn.id)

        assert cancelled.cancel_requested_at is not None
        assert app_server.sessions.interrupted == [("thread-act", "turn-live")]
    finally:
        db.close()


class FakeRuntimeSessions(FakeThreadSessions):
    def run_structured_turn(self, thread_id: str, message: str, _schema, **kwargs):
        assert thread_id == "thread-act"
        assert message == "Use Eidolon"
        kwargs["on_turn_started"]("turn-codex")
        return SimpleNamespace(
            output_text='{"response":"Done"}',
            items=[{"type": "mcpToolCall"}],
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.sessions = FakeRuntimeSessions()
        self.resumed: list[str] = []

    def resume_thread(self, _db, thread_id: str, **_kwargs) -> None:
        self.resumed.append(thread_id)


class MissingRolloutSessions(FakeThreadSessions):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[tuple[str, str]] = []

    def run_structured_turn(self, thread_id: str, message: str, _schema, **kwargs):
        self.inputs.append((thread_id, message))
        kwargs["on_turn_started"]("turn-recovered")
        return SimpleNamespace(output_text='{"response":"Recovered"}', items=[])


class MissingRolloutRuntime:
    def __init__(self) -> None:
        self.sessions = MissingRolloutSessions()
        self.started = 0

    def resume_thread(self, _db, _thread_id: str, **_kwargs) -> None:
        raise RuntimeError("no rollout found for thread id missing-thread")

    def start_thread(self, _db, **_kwargs) -> str:
        self.started += 1
        return "thread-recovered"


def test_dispatcher_resumes_and_completes_queued_turn(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
) -> None:
    db: Session = session_factory()
    db.add(ActSession(codex_thread_id="thread-act"))
    db.commit()
    session = db.query(ActSession).one()
    db.add(ActTurn(session_id=session.id, user_message="Use Eidolon", status="queued"))
    db.commit()
    db.close()
    runtime = FakeRuntime()
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", session_factory)
    monkeypatch.setattr("app.services.act_turn_dispatcher.act_app_server_service", runtime)
    monkeypatch.setattr(
        "app.services.act_turn_dispatcher.CodexRoutingService.resolve",
        lambda *_args, **_kwargs: SimpleNamespace(
            effective_model="gpt-act",
            effective_reasoning_effort="medium",
        ),
    )

    assert ActTurnDispatcher().process_next() is True

    verify: Session = session_factory()
    try:
        turn = verify.query(ActTurn).one()
        assert runtime.resumed == ["thread-act"]
        assert turn.codex_turn_id == "turn-codex"
        assert turn.status == "succeeded"
        assert turn.assistant_message == "Done"
        assert turn.activity_json == [{"kind": "mcpToolCall", "label": "Used an Eidolon tool"}]
    finally:
        verify.close()


def test_dispatcher_replaces_missing_rollout_without_replaying_completed_turns(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
) -> None:
    db: Session = session_factory()
    session = ActSession(codex_thread_id="missing-thread")
    db.add(session)
    db.flush()
    db.add_all(
        [
            ActTurn(
                session_id=session.id,
                user_message="Remember my goal",
                assistant_message="Your goal is to ship Eidolon",
                status="succeeded",
            ),
            ActTurn(session_id=session.id, user_message="What is my goal?", status="queued"),
        ]
    )
    db.commit()
    db.close()
    runtime = MissingRolloutRuntime()
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", session_factory)
    monkeypatch.setattr("app.services.act_turn_dispatcher.act_app_server_service", runtime)
    monkeypatch.setattr(
        "app.services.act_turn_dispatcher.CodexRoutingService.resolve",
        lambda *_args, **_kwargs: SimpleNamespace(
            effective_model="gpt-act",
            effective_reasoning_effort="medium",
        ),
    )

    assert ActTurnDispatcher().process_next() is True

    verify: Session = session_factory()
    try:
        refreshed_session = verify.query(ActSession).one()
        turns = verify.query(ActTurn).order_by(ActTurn.id).all()
        assert refreshed_session.codex_thread_id == "thread-recovered"
        assert runtime.started == 1
        assert runtime.sessions.inputs[0][0] == "thread-recovered"
        assert "<historical_conversation>" in runtime.sessions.inputs[0][1]
        assert "Current user request: What is my goal?" in runtime.sessions.inputs[0][1]
        assert turns[0].status == "succeeded"
        assert turns[1].status == "succeeded"
        assert turns[1].assistant_message == "Recovered"
        assert turns[1].activity_json[0] == {
            "kind": "threadRecovery",
            "label": "Recovered the Act session",
        }
    finally:
        verify.close()


def test_dispatcher_recovery_marks_only_running_turns_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    session_factory,
) -> None:
    db: Session = session_factory()
    session = ActSession(codex_thread_id="thread-act")
    db.add(session)
    db.flush()
    db.add_all(
        [
            ActTurn(session_id=session.id, user_message="running", status="running"),
            ActTurn(session_id=session.id, user_message="queued", status="queued"),
        ]
    )
    db.commit()
    db.close()
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", session_factory)

    assert ActTurnDispatcher().recover() == 1

    verify: Session = session_factory()
    try:
        statuses = {turn.user_message: turn.status for turn in verify.query(ActTurn).all()}
        assert statuses == {"running": "interrupted", "queued": "queued"}
    finally:
        verify.close()
