from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    ActSession,
    ActTurn,
    AgentProposal,
)
from app.services.act_session_service import (
    ActSessionError,
    ActSessionService,
)
from app.services.agent_proposal_service import AgentProposalService


class FakeThreadSessions:
    def __init__(self) -> None:
        self.archived: list[str] = []

    def archive_thread(self, thread_id: str) -> None:
        self.archived.append(thread_id)


class FakeAppServer:
    def __init__(self) -> None:
        self.sessions = FakeThreadSessions()


@pytest.fixture()
def db(tmp_path) -> Session:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-sessions.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def isolate_proposal_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AgentProposalService, "mirror", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(AgentProposalService, "_telegram", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.services.act_turn_dispatcher.act_turn_dispatcher.notify", lambda: None)


def test_assistant_reuses_one_permanent_session_across_all_origins(db: Session) -> None:
    service = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant")  # type: ignore[arg-type]
    origins = ["web", "telegram", "assessment", "web", "telegram", "assessment", "web"]
    created_ids = [service.create_session(origin=origin).id for origin in origins]

    retained = list(
        db.scalars(
            select(ActSession)
            .where(ActSession.agent_id == "assistant")
            .order_by(ActSession.created_at, ActSession.id)
        )
    )

    assert len(set(created_ids)) == 1
    assert [session.id for session in retained] == [created_ids[0]]
    assert retained[0].origin == origins[0]


def test_concurrent_assistant_creation_preserves_history(db: Session) -> None:
    service = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant")
    for _ in range(4):
        service.create_session()
    factory = sessionmaker(bind=db.get_bind())

    def create():
        with factory() as other:
            return ActSessionService(other, app_server=FakeAppServer(), agent_id="assistant").create_session().id

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(lambda _: create(), range(2)))
    db.expire_all()
    retained = list(db.scalars(select(ActSession.id).where(ActSession.agent_id == "assistant")))
    assert len(retained) == 1
    assert all(session_id in retained for session_id in created)


def test_assistant_can_create_session_while_oldest_is_busy(db: Session) -> None:
    service = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant")  # type: ignore[arg-type]
    sessions = [service.create_session() for _ in range(5)]
    db.add(ActTurn(session_id=sessions[0].id, user_message="Still working", status="running"))
    db.commit()

    service.create_session(origin="assistant")

    assert db.scalar(select(func.count()).select_from(ActSession).where(ActSession.agent_id == "assistant")) == 1
    assert db.get(ActSession, sessions[0].id) is not None


def test_proposal_history_survives_source_session_retention(db: Session) -> None:
    service = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant")
    source = service.create_session(origin="assessment")
    proposal = AgentProposal(
        source_session_id=source.id, title="Finish the report", rationale="Useful",
        instruction="Complete the report.", actions="Complete the report.",
        references_json=[], fingerprint=hashlib.sha256(b"retained").hexdigest(),
    )
    db.add(proposal)
    db.commit()
    for _ in range(5):
        service.create_session(origin="web")
    db.expire_all()
    assert db.get(ActSession, source.id) is not None
    assert db.get(AgentProposal, proposal.id).instruction == "Complete the report."


def test_agent_session_service_denies_cross_agent_reads(db: Session) -> None:
    observer = ActSessionService(db, app_server=FakeAppServer(), agent_id="observer").create_session()  # type: ignore[arg-type]

    with pytest.raises(ActSessionError, match="not found"):
        ActSessionService(db, app_server=FakeAppServer(), agent_id="act").read_session(observer.id)  # type: ignore[arg-type]


def test_replayed_proposal_approval_creates_exactly_one_act_session_and_turn(db: Session) -> None:
    assistant = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant").create_session()  # type: ignore[arg-type]
    proposal = AgentProposal(
        source_session_id=assistant.id,
        title="Implement the next goal",
        rationale="The next action is ready.",
        instruction="Implement the next goal safely.",
        actions="Implement and verify.",
        references_json=["goal-1"],
        fingerprint=hashlib.sha256(b"proposal-one").hexdigest(),
    )
    db.add(proposal)
    db.commit()

    service = AgentProposalService(db)
    first = service.decide(proposal.id, approve=True)
    replay = service.decide(proposal.id, approve=True)

    act_sessions = list(db.scalars(select(ActSession).where(ActSession.agent_id == "act")))
    act_turns = list(
        db.scalars(
            select(ActTurn).where(ActTurn.session_id.in_(select(ActSession.id).where(ActSession.agent_id == "act")))
        )
    )
    assert first.status == "approved"
    assert replay.id == first.id
    assert replay.act_session_id == first.act_session_id
    assert len(act_sessions) == 1
    assert len(act_turns) == 1
    assert act_turns[0].user_message == proposal.instruction
    assert act_turns[0].status == "queued"
