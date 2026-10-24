"""Daily Assistant continuity, refresh boundaries, and permanent chat ownership."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ActLiveMessage, ActSession, ActTurn, AssistantState
from app.services.act_session_service import ActSessionService
from app.services.agent_memory_service import AgentMemoryService
from app.services.assistant_service import AssistantService
from app.services.assistant_session_clock import refresh_boundary, refresh_is_due
from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher


class Clock(datetime):
    current = datetime(2026, 10, 17, 3, 30, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class Runtime:
    def __init__(self):
        self.sessions = self
        self.live = None
        self.started = []
        self.resumed = []
        self.inputs = []
        self.stopped = 0
        self.on_run = None
        self.on_start = None
        self.missing_on_resume = False
        self.missing_on_run = False
        self.credential_valid = True

    def has_live_thread(self, _db, thread_id, session_id):
        return self.live == (thread_id, session_id) and self.credential_valid

    def start_thread(self, _db, *, session_id, **_kwargs):
        thread_id = f"daily-thread-{len(self.started) + 1}"
        self.started.append(thread_id)
        self.live = (thread_id, session_id)
        if self.on_start:
            self.on_start()
        return thread_id

    def resume_thread(self, _db, thread_id, *, session_id, **_kwargs):
        self.resumed.append(thread_id)
        if self.missing_on_resume:
            raise RuntimeError(f"no rollout found for thread id {thread_id}")
        self.live = (thread_id, session_id)
        self.credential_valid = True

    def run_structured_turn(self, thread_id, message, _schema, **kwargs):
        self.inputs.append((thread_id, message))
        if self.missing_on_run:
            self.missing_on_run = False
            raise RuntimeError(f"no rollout found for thread id {thread_id}")
        kwargs["on_turn_started"](f"codex-turn-{len(self.inputs)}")
        if self.on_run:
            self.on_run()
        return SimpleNamespace(output_text='{"response":"Done","title":null}', items=[])

    def stop(self):
        self.stopped += 1
        self.live = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'daily.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    runtime = Runtime()
    dispatcher = AssistantTurnDispatcher()
    Clock.current = datetime(2026, 10, 17, 3, 30, tzinfo=UTC)
    monkeypatch.setattr("app.services.assistant_turn_dispatcher.datetime", Clock)
    monkeypatch.setattr("app.services.act_turn_dispatcher.datetime", Clock)
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", factory)
    monkeypatch.setattr("app.services.assistant_turn_dispatcher.SessionLocal", factory)
    monkeypatch.setattr("app.services.act_turn_dispatcher.get_agent_app_server", lambda *_: runtime)
    monkeypatch.setattr("app.services.act_app_server_service.get_agent_app_server", lambda *_: runtime)
    monkeypatch.setattr("app.services.act_turn_dispatcher.CodexRoutingService.resolve",
                        lambda *_a, **_k: SimpleNamespace(effective_model="fake", effective_reasoning_effort="high"))
    monkeypatch.setattr("app.services.act_turn_dispatcher.AgentPolicyService.policy",
                        lambda *_: SimpleNamespace(model=None, reasoning_effort=None))
    monkeypatch.setattr("app.services.act_turn_dispatcher.AgentPolicyService.revoke", lambda *_: None)
    monkeypatch.setattr("app.services.act_turn_dispatcher.TitleSyncService.sync_from_codex", lambda *_: None)
    monkeypatch.setattr("app.services.agent_proposal_service.AgentProposalService.refresh_execution", lambda *_: None)
    monkeypatch.setattr(AssistantTurnDispatcher, "_finish_turn", lambda *_: None)
    monkeypatch.setattr(AssistantTurnDispatcher, "_after_turn", lambda *_: None)
    monkeypatch.setattr("app.services.assistant_turn_dispatcher.act_browser_runtime.revoke_session", lambda *_: None)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr(AssistantService, "context", lambda *_: {
        "memory": {"daily_summaries": [{"summary": "Explicit daily summary"}]},
    })
    monkeypatch.setattr("app.services.agent_dispatchers.agent_dispatchers", {"assistant": dispatcher})
    try:
        yield SimpleNamespace(factory=factory, runtime=runtime, dispatcher=dispatcher)
    finally:
        engine.dispose()


def primary(env, *, thread="pending:initial", started=None):
    with env.factory() as db:
        service = AssistantService(db)
        session = service.primary_session()
        session.codex_thread_id = thread
        state = service.state()
        state.thread_started_at = started
        state.timezone = "America/Toronto"
        db.commit()
        return session.id


def queue(env, session_id, text="Current request"):
    with env.factory() as db:
        turn = ActTurn(session_id=session_id, user_message=text, status="queued", created_at=Clock.current)
        db.add(turn)
        db.commit()
        return turn.id


def process(env, session_id, text="Current request"):
    turn_id = queue(env, session_id, text)
    assert env.dispatcher.process_next()
    with env.factory() as db:
        turn = db.get(ActTurn, turn_id)
        assert turn.status == "succeeded", turn.error_message
    return turn_id


def test_local_midnight_keeps_same_session_until_four_am(env):
    Clock.current = datetime(2026, 10, 16, 23, 30, tzinfo=UTC)
    session_id = primary(env)
    process(env, session_id, "First request")
    for hour, minute in [(3, 59), (4, 0), (7, 59)]:
        # Toronto crosses midnight at 04:00 UTC; its refresh is 08:00 UTC.
        Clock.current = datetime(2026, 10, 17, hour, minute, tzinfo=UTC)
        process(env, session_id, f"Request at {hour}:{minute}")
    assert env.runtime.started == ["daily-thread-1"]
    assert env.runtime.resumed == []
    assert env.runtime.stopped == 0
    assert [thread for thread, _ in env.runtime.inputs] == ["daily-thread-1"] * 4


def test_four_am_creates_fresh_thread_in_same_chat(env):
    session_id = primary(env)
    process(env, session_id)
    Clock.current = datetime(2026, 10, 17, 8, 0, tzinfo=UTC)
    process(env, session_id, "New day request")
    assert env.runtime.started == ["daily-thread-1", "daily-thread-2"]
    assert env.runtime.resumed == []
    with env.factory() as db:
        assert [s.id for s in db.scalars(select(ActSession))] == [session_id]
        assert db.get(AssistantState, 1).primary_session_id == session_id
        assert len(list(db.scalars(select(ActTurn)))) == 2


def test_restart_resumes_same_day_without_starting_fresh_thread(env):
    session_id = primary(env, thread="persisted-thread", started=Clock.current - timedelta(hours=1))
    process(env, session_id)
    process(env, session_id)
    assert env.runtime.resumed == ["persisted-thread"]
    assert env.runtime.started == []
    assert env.runtime.stopped == 0


def test_revoked_live_credential_resumes_same_thread_before_next_turn(env):
    session_id = primary(env)
    process(env, session_id, "First request")
    env.runtime.credential_valid = False
    process(env, session_id, "Continue Atlas initialization")
    process(env, session_id, "Next request")
    assert env.runtime.started == ["daily-thread-1"]
    assert env.runtime.resumed == ["daily-thread-1"]
    assert [thread for thread, _ in env.runtime.inputs] == ["daily-thread-1"] * 3


def test_refresh_during_running_turn_waits_until_next_boundary(env):
    session_id = primary(env)

    def refresh_during_run():
        env.runtime.on_run = None
        with env.factory() as db:
            assert db.scalar(select(ActTurn.status)) == "running"
            response = AssistantService(db).request_refresh()
            assert response == {"status": "refresh_pending", "session_id": session_id}
            assert db.get(ActSession, session_id).codex_thread_id == "daily-thread-1"

    env.runtime.on_run = refresh_during_run
    process(env, session_id)
    assert env.runtime.started == ["daily-thread-1"]
    assert env.runtime.stopped == 0
    with env.factory() as db:
        assert db.get(AssistantState, 1).refresh_pending
    process(env, session_id, "After refresh")
    assert env.runtime.started == ["daily-thread-1", "daily-thread-2"]
    with env.factory() as db:
        assert not db.get(AssistantState, 1).refresh_pending


def test_refresh_requested_during_thread_start_is_not_lost(env):
    session_id = primary(env)

    def refresh_during_start():
        env.runtime.on_start = None
        with env.factory() as db:
            AssistantService(db).request_refresh()

    env.runtime.on_start = refresh_during_start
    process(env, session_id)
    with env.factory() as db:
        assert db.get(AssistantState, 1).refresh_pending
    process(env, session_id)
    assert env.runtime.started == ["daily-thread-1", "daily-thread-2"]


@pytest.mark.parametrize("missing_at", ["resume", "run"])
def test_missing_rollout_recovery_does_not_replay_any_chat_history(env, missing_at):
    session_id = primary(env, thread="missing-thread", started=Clock.current - timedelta(hours=1))
    with env.factory() as db:
        other = ActSession(agent_id="assistant", codex_thread_id="legacy-thread", status="archived")
        db.add(other)
        db.flush()
        db.add_all([
            ActTurn(session_id=session_id, user_message="SECRET yesterday user", status="succeeded",
                    assistant_message="SECRET yesterday assistant", created_at=Clock.current - timedelta(days=1)),
            ActTurn(session_id=session_id, user_message="SECRET same-day old user", status="succeeded",
                    assistant_message="SECRET same-day old assistant", created_at=Clock.current - timedelta(minutes=30)),
            ActTurn(session_id=session_id, user_message="", status="succeeded", backend_message_kind="notice",
                    assistant_message="SECRET prior backend message", created_at=Clock.current - timedelta(days=1)),
            ActTurn(session_id=other.id, user_message="SECRET other conversation", status="succeeded",
                    assistant_message="SECRET other response", created_at=Clock.current),
        ])
        db.commit()
    setattr(env.runtime, f"missing_on_{missing_at}", True)
    process(env, session_id, "The only current request")
    assert env.runtime.started == ["daily-thread-1"]
    for _thread, message in env.runtime.inputs:
        assert "SECRET" not in message
        assert "historical_conversation" not in message
        assert "The only current request" in message
        assert "Explicit daily summary" in message


def test_fresh_daily_thread_excludes_previous_day_backend_messages(env):
    session_id = primary(env, thread="yesterday-thread", started=Clock.current - timedelta(days=1))
    with env.factory() as db:
        db.add(ActTurn(session_id=session_id, user_message="Yesterday chat", assistant_message="Yesterday answer",
                       status="succeeded", created_at=Clock.current - timedelta(days=1)))
        db.add(ActTurn(session_id=session_id, user_message="", assistant_message="Yesterday backend notice",
                       status="succeeded", backend_message_kind="notice", created_at=Clock.current - timedelta(days=1)))
        db.commit()
    process(env, session_id)
    assert env.runtime.started == ["daily-thread-1"]
    assert "Yesterday" not in env.runtime.inputs[0][1]


def test_concurrent_primary_creation_has_one_chat(env):
    barrier = Barrier(4)

    def create(_):
        with env.factory() as db:
            barrier.wait(timeout=5)
            return ActSessionService(db, agent_id="assistant").create_session().id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(create, range(4)))
    assert len(set(ids)) == 1
    with env.factory() as db:
        assert len(list(db.scalars(select(ActSession).where(ActSession.agent_id == "assistant")))) == 1
        assert db.get(AssistantState, 1).primary_session_id == ids[0]


def test_primary_migration_preserves_old_history_and_moves_only_queued_work(env):
    with env.factory() as db:
        older = ActSession(agent_id="assistant", codex_thread_id="older", updated_at=Clock.current - timedelta(days=1))
        newer = ActSession(agent_id="assistant", codex_thread_id="newer", updated_at=Clock.current)
        db.add_all([older, newer])
        db.flush()
        history = ActTurn(session_id=older.id, user_message="Preserve old history", status="succeeded")
        queued = ActTurn(session_id=older.id, user_message="Carry queued work", status="queued")
        db.add_all([history, queued])
        db.commit()
        result = AssistantService(db).primary_session()
        db.refresh(older)
        db.refresh(history)
        db.refresh(queued)
        assert result.id == newer.id
        assert older.status == "archived"
        assert history.session_id == older.id
        assert history.user_message == "Preserve old history"
        assert queued.session_id == newer.id
        assert len(list(db.scalars(select(ActTurn)))) == 2


def test_refresh_command_is_control_even_with_waiting_live_reply(env):
    session_id = primary(env, thread="live-thread", started=Clock.current)
    with env.factory() as db:
        running = ActTurn(session_id=session_id, user_message="Ongoing work", status="running")
        db.add(running)
        db.flush()
        waiting = ActLiveMessage(turn_id=running.id, role="assistant", text="Which account?",
                                 reply_status="awaiting", reply_expires_at=datetime.now(UTC) + timedelta(minutes=5))
        db.add(waiting)
        db.commit()
        control = ActSessionService(db, agent_id="assistant").enqueue_turn(session_id, " /REFRESH ")
        db.refresh(waiting)
        assert control.id != running.id
        assert control.status == "succeeded"
        assert control.backend_message_kind == "assistant_refresh"
        assert control.codex_turn_id is None
        assert db.get(AssistantState, 1).refresh_pending
        assert waiting.reply_status == "awaiting"
        assert len(list(db.scalars(select(ActLiveMessage)))) == 1
    assert not env.dispatcher.process_next()
    assert env.runtime.inputs == []


@pytest.mark.parametrize(("current", "expected", "following", "hours"), [
    (datetime(2026, 3, 7, 10, tzinfo=UTC), datetime(2026, 3, 7, 9, tzinfo=UTC),
     datetime(2026, 3, 8, 8, tzinfo=UTC), 23),
    (datetime(2026, 10, 31, 10, tzinfo=UTC), datetime(2026, 10, 31, 8, tzinfo=UTC),
     datetime(2026, 11, 1, 9, tzinfo=UTC), 25),
])
def test_four_am_boundary_tracks_dst_wall_time(current, expected, following, hours):
    assert refresh_boundary(current, "America/Toronto") == expected
    assert refresh_boundary(current, "America/Toronto", next_boundary=True) == following
    assert following - expected == timedelta(hours=hours)
    assert not refresh_is_due(expected, following - timedelta(microseconds=1), "America/Toronto")
    assert refresh_is_due(expected, following, "America/Toronto")
    assert not refresh_is_due(following, following, "America/Toronto")


def test_refresh_uses_configured_timezone(env):
    Clock.current = datetime(2026, 10, 17, 10, 59, tzinfo=UTC)
    session_id = primary(env, thread="vancouver-thread", started=datetime(2026, 10, 16, 12, tzinfo=UTC))
    with env.factory() as db:
        db.get(AssistantState, 1).timezone = "America/Vancouver"
        db.commit()
    assert not env.dispatcher.refresh_if_due()
    Clock.current = datetime(2026, 10, 17, 11, tzinfo=UTC)
    assert env.dispatcher.refresh_if_due()
    with env.factory() as db:
        assert db.get(ActSession, session_id).codex_thread_id == "daily-thread-1"
    assert env.runtime.inputs == []


@pytest.mark.parametrize("days_missed", [1, 4])
def test_startup_invalidates_missed_windows_and_idle_refresh_preserves_history(env, days_missed):
    Clock.current = datetime(2026, 10, 17, 8, 1, tzinfo=UTC)
    session_id = primary(env, thread="stale-before-restart", started=Clock.current - timedelta(days=days_missed))
    with env.factory() as db:
        old = ActTurn(session_id=session_id, user_message="SECRET missed-window history",
                      assistant_message="SECRET prior answer", status="succeeded",
                      created_at=Clock.current - timedelta(days=days_missed))
        db.add(old)
        db.commit()
        old_id = old.id
    assert env.dispatcher.recover() == 0
    with env.factory() as db:
        assert db.get(ActSession, session_id).codex_thread_id.startswith("pending:")
        assert db.get(AssistantState, 1).refresh_pending
        archived = db.get(ActTurn, old_id)
        assert archived.user_message == "" and archived.assistant_message is None
        assert AgentMemoryService(db).read_turn_content(archived)["user_message"] == "SECRET missed-window history"
    # Recovery persists invalidation before any external runtime operation.
    assert env.runtime.started == []
    assert env.runtime.resumed == []
    assert env.dispatcher.refresh_if_due()
    assert not env.dispatcher.refresh_if_due()
    assert env.runtime.inputs == []
    with env.factory() as db:
        assert len(list(db.scalars(select(ActTurn)))) == 1
        assert db.get(AssistantState, 1).primary_session_id == session_id
        assert not db.get(AssistantState, 1).refresh_pending
    process(env, session_id, "Current request after startup")
    assert env.runtime.started == ["daily-thread-1"]
    assert env.runtime.resumed == []
    assert "SECRET" not in env.runtime.inputs[0][1]


def test_startup_within_same_four_am_window_keeps_saved_thread(env):
    Clock.current = datetime(2026, 10, 17, 7, 59, tzinfo=UTC)
    session_id = primary(env, thread="same-window-thread", started=datetime(2026, 10, 16, 9, tzinfo=UTC))
    assert env.dispatcher.recover() == 0
    assert not env.dispatcher.refresh_if_due()
    with env.factory() as db:
        assert db.get(ActSession, session_id).codex_thread_id == "same-window-thread"
        assert not db.get(AssistantState, 1).refresh_pending
    process(env, session_id)
    assert env.runtime.resumed == ["same-window-thread"]
    assert env.runtime.started == []


def test_idle_maintenance_defers_four_am_refresh_until_running_turn_finishes(env):
    Clock.current = datetime(2026, 10, 17, 8, tzinfo=UTC)
    session_id = primary(env, thread="running-thread", started=Clock.current - timedelta(hours=1))
    with env.factory() as db:
        running = ActTurn(session_id=session_id, user_message="Crossing 04:00", status="running")
        db.add(running)
        db.commit()
        turn_id = running.id
    assert not env.dispatcher.refresh_if_due()
    assert env.runtime.started == []
    with env.factory() as db:
        assert db.get(ActSession, session_id).codex_thread_id == "running-thread"
        db.get(ActTurn, turn_id).status = "succeeded"
        db.commit()
    assert env.dispatcher.refresh_if_due()
    assert env.runtime.started == ["daily-thread-1"]
    assert env.runtime.inputs == []


def test_idle_refresh_failure_keeps_durable_pending_state_and_retries_after_thirty_seconds(env):
    Clock.current = datetime(2026, 10, 17, 8, tzinfo=UTC)
    session_id = primary(env, thread="expired-thread", started=Clock.current - timedelta(days=1))

    def unavailable():
        raise RuntimeError("Runtime temporarily unavailable")

    env.runtime.on_start = unavailable
    assert not env.dispatcher.refresh_if_due()
    with env.factory() as db:
        assert db.get(AssistantState, 1).refresh_pending
        assert db.get(ActSession, session_id).codex_thread_id.startswith("pending:")
    assert env.runtime.resumed == []
    env.runtime.on_start = None
    Clock.current += timedelta(seconds=29)
    assert not env.dispatcher.refresh_if_due()
    assert len(env.runtime.started) == 1
    Clock.current += timedelta(seconds=1)
    assert env.dispatcher.refresh_if_due()
    assert len(env.runtime.started) == 2
    assert env.runtime.inputs == []
    with env.factory() as db:
        assert not db.get(AssistantState, 1).refresh_pending
        assert db.get(ActSession, session_id).codex_thread_id == "daily-thread-2"


def test_dispatch_loop_checks_idle_refresh_before_queued_turn(env, monkeypatch):
    operations = []

    def maintenance():
        operations.append("refresh")

    def process_next():
        operations.append("turn")
        env.dispatcher._stop.set()
        return True

    monkeypatch.setattr(env.dispatcher, "refresh_if_due", maintenance)
    monkeypatch.setattr(env.dispatcher, "process_next", process_next)
    env.dispatcher._run()
    assert operations == ["refresh", "turn"]


def test_existing_assistant_state_schema_upgrade_is_idempotent(env, monkeypatch):
    from sqlalchemy import text

    from app import db as database

    with env.factory() as db:
        state = AssistantService(db).state()
        state.summary = "Keep durable understanding"
        db.commit()
        engine = db.get_bind()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE assistant_state DROP COLUMN thread_started_at"))
        connection.execute(text("ALTER TABLE assistant_state DROP COLUMN refresh_pending"))
    monkeypatch.setattr(database, "engine", engine)
    database.ensure_local_schema()
    database.ensure_local_schema()
    with env.factory() as db:
        upgraded = db.get(AssistantState, 1)
        assert upgraded.summary == "Keep durable understanding"
        assert upgraded.thread_started_at is None
        assert upgraded.refresh_pending is False
