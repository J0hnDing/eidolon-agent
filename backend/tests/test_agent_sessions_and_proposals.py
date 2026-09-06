from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    ActSession,
    ActTelegramBinding,
    ActTurn,
    AgentProposal,
    TelegramBotConnection,
)
from app.services.act_session_service import (
    ActSessionError,
    ActSessionService,
    AssistantSessionCapacityError,
)
from app.services.agent_policy_service import current_agent
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


def test_assistant_retains_five_most_recent_sessions_across_all_origins(db: Session) -> None:
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

    assert [session.id for session in retained] == created_ids[-5:]
    assert [session.origin for session in retained] == origins[-5:]


def test_concurrent_assistant_creation_cannot_exceed_five(db: Session) -> None:
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
    assert len(retained) == 5
    assert all(session_id in retained for session_id in created)


def test_assistant_rejects_sixth_session_when_oldest_is_busy(db: Session) -> None:
    service = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant")  # type: ignore[arg-type]
    sessions = [service.create_session() for _ in range(5)]
    db.add(ActTurn(session_id=sessions[0].id, user_message="Still working", status="running"))
    db.commit()

    with pytest.raises(AssistantSessionCapacityError, match="oldest session is busy"):
        service.create_session(origin="assessment")

    assert db.scalar(select(func.count()).select_from(ActSession).where(ActSession.agent_id == "assistant")) == 5
    assert db.get(ActSession, sessions[0].id) is not None


def test_pruning_oldest_assistant_session_clears_telegram_selection(db: Session) -> None:
    service = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant")  # type: ignore[arg-type]
    sessions = [service.create_session(origin="telegram") for _ in range(5)]
    connection = TelegramBotConnection(
        role="assistant_agent",
        secret_store_id="test",
        secret_reference="test/assistant",
        bot_id="assistant-bot",
        status="paired",
        paired_chat_id="100",
        paired_user_id="200",
    )
    db.add(connection)
    db.flush()
    db.add(ActTelegramBinding(connection_id=connection.id, active_session_id=sessions[0].id))
    db.commit()

    service.create_session(origin="web")

    db.expire_all()
    assert db.get(ActTelegramBinding, connection.id).active_session_id is None
    assert db.get(ActSession, sessions[0].id) is None


def test_proposal_history_survives_source_session_retention(db: Session) -> None:
    service = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant")  # type: ignore[arg-type]
    source = service.create_session(origin="assessment")
    token = current_agent.set(("assistant", source.id))
    try:
        proposal = AgentProposalService(db).submit(
            source.id,
            {
                "title": "Finish the report",
                "rationale": "The remaining section is bounded and ready.",
                "instruction": "Complete the remaining report section.",
                "actions": "Review the draft and finish the remaining section.",
                "references": ["TODO-12"],
            },
        )
    finally:
        current_agent.reset(token)
    for _ in range(4):
        service.create_session(origin="web")

    service.create_session(origin="telegram")

    db.expire_all()
    assert db.get(ActSession, source.id) is None
    retained_proposal = db.get(AgentProposal, proposal.id)
    assert retained_proposal is not None
    assert retained_proposal.source_session_id == source.id
    assert AgentProposalService(db).list()[0]["instruction"] == "Complete the remaining report section."


def test_agent_session_service_denies_cross_agent_reads(db: Session) -> None:
    observer = ActSessionService(db, app_server=FakeAppServer(), agent_id="observer").create_session()  # type: ignore[arg-type]

    with pytest.raises(ActSessionError, match="not found"):
        ActSessionService(db, app_server=FakeAppServer(), agent_id="act").read_session(observer.id)  # type: ignore[arg-type]


def test_thread_proposal_limit_excludes_duplicates_and_unlimited_replacements(db: Session) -> None:
    source = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant").create_session()
    service = AgentProposalService(db)
    token = current_agent.set(("assistant", source.id))
    arguments = {"title": "Plan", "rationale": "Useful", "actions": "Work", "instruction": "Plan 0"}
    try:
        originals = [service.submit(source.id, {**arguments, "instruction": f"Plan {i}"}) for i in range(5)]
        assert service.submit(source.id, arguments).id == originals[0].id
        for i in range(7):
            service.submit(source.id, {
                **arguments, "instruction": f"Replacement {i}",
                "replaces_proposal_id": originals[0].id, "material_change": "Updated requirements",
            })
        # Deleting history must not replenish the thread's lifetime allowance.
        db.delete(originals[-1])
        db.commit()
        with pytest.raises(ValueError, match="limit of 5"):
            service.submit(source.id, {**arguments, "instruction": "Sixth new plan"})
        assert db.get(ActSession, source.id).proposal_count == 5
    finally:
        current_agent.reset(token)


def test_concurrent_proposals_cannot_claim_the_same_last_slot(db: Session) -> None:
    source = ActSessionService(db, app_server=FakeAppServer(), agent_id="assistant").create_session()
    source.proposal_count = 4
    db.commit()
    session_id = source.id
    factory = sessionmaker(bind=db.get_bind())

    def submit(number):
        with factory() as other:
            token = current_agent.set(("assistant", session_id))
            try:
                AgentProposalService(other).submit(session_id, {
                    "title": "Plan", "rationale": "Useful", "actions": "Work", "instruction": f"Plan {number}",
                })
                return "created"
            except ValueError as exc:
                assert "limit of 5" in str(exc)
                return "limited"
            finally:
                current_agent.reset(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == ["created", "limited"]
    db.expire_all()
    assert db.get(ActSession, session_id).proposal_count == 5


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
