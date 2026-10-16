from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ActSession, ActTurn, AgentCredential
from app.services.act_browser_runtime_service import ActBrowserRuntimeError
from app.services.act_session_service import ActSessionService
from app.services.act_turn_dispatcher import ActTurnDispatcher
from app.services.agent_policy_service import AgentPolicyService


class BlockingRuntime:
    def __init__(self) -> None:
        self.sessions = self
        self.started = threading.Event()
        self.release = threading.Event()
        self.messages: list[str] = []
        self.interrupted: list[tuple[str, str]] = []
        self.active = 0
        self.maximum_active = 0

    def resume_thread(self, _db, _thread_id: str, **_kwargs) -> None:
        pass

    def run_structured_turn(self, thread_id: str, message: str, _schema, **kwargs):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            assert message.startswith("<eidolon_session_metadata>")
            self.messages.append(message.split("\n\n", 1)[1])
            kwargs["on_turn_started"](f"turn-{thread_id}")
            self.started.set()
            assert self.release.wait(8), "Test turn was never released"
            return SimpleNamespace(output_text='{"response":"Done"}', items=[])
        finally:
            self.active -= 1

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.interrupted.append((thread_id, turn_id))
        self.release.set()

    def stop(self) -> None:
        self.release.set()


@pytest.fixture
def concurrent_dispatcher(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    runtimes: dict[int, BlockingRuntime] = {}
    monkeypatch.setattr("app.services.act_turn_dispatcher.SessionLocal", factory)
    monkeypatch.setattr(
        "app.services.act_turn_dispatcher.get_agent_app_server",
        lambda _agent_id, session_id: runtimes[session_id],
    )
    monkeypatch.setattr(
        "app.services.act_session_service.get_agent_app_server",
        lambda _agent_id, session_id: runtimes[session_id],
    )
    monkeypatch.setattr(
        "app.services.act_turn_dispatcher.stop_act_app_servers",
        lambda: [runtime.stop() for runtime in runtimes.values()],
    )
    monkeypatch.setattr(
        "app.services.act_turn_dispatcher.CodexRoutingService.resolve",
        lambda *_args, **_kwargs: SimpleNamespace(effective_model=None, effective_reasoning_effort=None),
    )
    monkeypatch.setattr("app.services.act_turn_dispatcher.TitleSyncService.sync_from_codex", lambda *_args: None)
    dispatcher = ActTurnDispatcher()
    monkeypatch.setattr(dispatcher, "_deliver", lambda _turn_id: None)
    try:
        yield dispatcher, factory, runtimes
    finally:
        for runtime in runtimes.values():
            runtime.release.set()
        dispatcher.stop()
        engine.dispose()


def queue_sessions(factory, runtimes):
    with factory() as db:
        sessions = [ActSession(codex_thread_id=f"thread-{name}") for name in ("first", "second")]
        db.add_all(sessions)
        db.flush()
        turns = [ActTurn(session_id=session.id, user_message="First request", status="queued") for session in sessions]
        db.add_all(turns)
        db.commit()
        ids = [session.id for session in sessions]
        turn_ids = [turn.id for turn in turns]
    runtimes.update({session_id: BlockingRuntime() for session_id in ids})
    return ids, turn_ids


def wait_for_statuses(factory, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with factory() as db:
            actual = dict(db.execute(select(ActTurn.id, ActTurn.status)).all())
        if all(actual.get(turn_id) == status for turn_id, status in expected.items()):
            return
        threading.Event().wait(0.02)
    pytest.fail(f"Expected {expected}, got {actual}")


def test_different_sessions_overlap_but_one_sessions_turns_stay_sequential(concurrent_dispatcher):
    dispatcher, factory, runtimes = concurrent_dispatcher
    ids, turn_ids = queue_sessions(factory, runtimes)
    with factory() as db:
        # Exercise serialization even if multiple rows were queued by a race.
        following = ActTurn(session_id=ids[0], user_message="Following request", status="queued")
        db.add(following)
        db.commit()
        following_id = following.id

    dispatcher.start()
    assert runtimes[ids[0]].started.wait(5)
    assert runtimes[ids[1]].started.wait(5), "Second Act session waited behind the first"
    assert runtimes[ids[0]].messages == ["First request"]
    with factory() as db:
        assert db.get(ActTurn, following_id).status == "queued"
        assert all(db.get(ActTurn, turn_id).status == "running" for turn_id in turn_ids)

    for runtime in runtimes.values():
        runtime.release.set()
    wait_for_statuses(factory, {turn_id: "succeeded" for turn_id in [*turn_ids, following_id]})
    assert runtimes[ids[0]].messages == ["First request", "Following request"]
    assert all(runtime.maximum_active == 1 for runtime in runtimes.values())


def test_cancelling_one_running_session_does_not_interrupt_another(concurrent_dispatcher):
    dispatcher, factory, runtimes = concurrent_dispatcher
    ids, turn_ids = queue_sessions(factory, runtimes)
    dispatcher.start()
    assert all(runtimes[session_id].started.wait(5) for session_id in ids)

    with factory() as db:
        ActSessionService(db).cancel_turn(ids[0], turn_ids[0])
    wait_for_statuses(factory, {turn_ids[0]: "cancelled", turn_ids[1]: "running"})
    assert runtimes[ids[0]].interrupted == [("thread-first", "turn-thread-first")]
    assert runtimes[ids[1]].interrupted == []
    assert not runtimes[ids[1]].release.is_set()

    runtimes[ids[1]].release.set()
    wait_for_statuses(factory, {turn_ids[0]: "cancelled", turn_ids[1]: "succeeded"})


def test_shutdown_interrupts_all_running_sessions(concurrent_dispatcher):
    dispatcher, factory, runtimes = concurrent_dispatcher
    ids, turn_ids = queue_sessions(factory, runtimes)
    dispatcher.start()
    assert all(runtimes[session_id].started.wait(5) for session_id in ids)

    dispatcher.stop()

    wait_for_statuses(factory, {turn_id: "cancelled" for turn_id in turn_ids})
    assert all(len(runtime.interrupted) == 1 for runtime in runtimes.values())
    assert dispatcher._session_workers == {}


@pytest.mark.parametrize("browser_unavailable", [False, True])
def test_completed_turn_revokes_access_without_closing_pages(concurrent_dispatcher, monkeypatch, browser_unavailable):
    dispatcher, factory, runtimes = concurrent_dispatcher
    ids, turn_ids = queue_sessions(factory, runtimes)
    revoked = []

    def revoke(session_id):
        revoked.append(session_id)
        if browser_unavailable:
            raise ActBrowserRuntimeError("Shared Act browser is unavailable")

    monkeypatch.setattr("app.services.act_turn_dispatcher.act_browser_runtime.revoke_session", revoke)
    monkeypatch.setattr("app.services.act_turn_dispatcher.act_browser_runtime.close_session",
                        lambda _sid: pytest.fail("Completing a turn must preserve browser pages"))
    with factory() as db:
        AgentPolicyService(db).issue("act", ids[0])
    runtimes[ids[0]].release.set()

    assert dispatcher.process_next(session_id=ids[0])

    assert revoked == [ids[0]]
    with factory() as db:
        assert db.get(ActTurn, turn_ids[0]).status == "succeeded"
        assert all(row.revoked for row in db.scalars(select(AgentCredential).where(AgentCredential.session_id == ids[0])))
        assert db.get(ActTurn, turn_ids[1]).status == "queued"
