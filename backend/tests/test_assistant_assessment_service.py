from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import AssistantAssessmentState, ScheduleOccurrence
from app.services import assistant_assessment_service
from app.services.act_session_service import AssistantSessionCapacityError
from app.services.assistant_assessment_service import (
    ASSISTANT_ASSESSMENT_INSTRUCTION,
    AssistantAssessmentError,
    AssistantAssessmentService,
)
from app.services.scheduler_service import (
    ASSISTANT_ASSESSMENT_JOB_ID,
    SchedulerService,
)


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def add_job(
        self,
        func,
        trigger,
        id: str,
        args: list[Any],
        replace_existing: bool,
        max_instances: int,
        coalesce: bool,
        misfire_grace_time: int | None = None,
    ) -> None:
        self.jobs[id] = {
            "func": func,
            "trigger": trigger,
            "args": args,
            "replace_existing": replace_existing,
            "max_instances": max_instances,
            "coalesce": coalesce,
            "misfire_grace_time": misfire_grace_time,
        }

    def get_job(self, id: str):
        return SimpleNamespace() if id in self.jobs else None

    def remove_job(self, id: str) -> None:
        self.jobs.pop(id, None)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def test_configure_starts_first_assessment_after_72_hours_and_resume_reanchors(
    db_session: Session,
) -> None:
    enabled_at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    service = AssistantAssessmentService(db_session, now=lambda: enabled_at)

    assert service.status() == {
        "enabled": False,
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
    }
    enabled = service.configure(True)

    assert enabled["enabled"] is True
    assert enabled["next_run_at"].replace(tzinfo=UTC) == enabled_at + timedelta(days=3)
    assert service.configure(False)["next_run_at"] is None

    resumed_at = enabled_at + timedelta(days=1)
    resumed = AssistantAssessmentService(db_session, now=lambda: resumed_at).configure(True)
    assert resumed["next_run_at"].replace(tzinfo=UTC) == resumed_at + timedelta(days=3)


def test_assessment_instruction_requires_grounded_proactive_context_and_proposals() -> None:
    assert ASSISTANT_ASSESSMENT_INSTRUCTION.startswith("Act as a proactive personal assistant.")
    assert r"knowledge\assistant" in ASSISTANT_ASSESSMENT_INSTRUCTION
    assert "internet search" in ASSISTANT_ASSESSMENT_INSTRUCTION
    assert "State any material assumptions in the proposal." in ASSISTANT_ASSESSMENT_INSTRUCTION
    assert "expected benefit justifies interrupting the user" in ASSISTANT_ASSESSMENT_INSTRUCTION
    assert "read the Assistant proposal history" in ASSISTANT_ASSESSMENT_INSTRUCTION
    assert "finish quietly without submitting a proposal" in ASSISTANT_ASSESSMENT_INSTRUCTION


def test_run_now_creates_fresh_assistant_session_and_queues_assessment(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeActSessionService:
        def __init__(self, db: Session, *, agent_id: str) -> None:
            calls.append(("init", agent_id))

        def create_session(self, *, origin: str, commit: bool):
            calls.append(("create", origin))
            assert commit is False
            return SimpleNamespace(id=41)

        def enqueue_turn(self, session_id: int, message: str, *, commit: bool):
            calls.append(("enqueue", session_id, message))
            assert commit is True
            return SimpleNamespace(id=73)

    monkeypatch.setattr(assistant_assessment_service, "ActSessionService", FakeActSessionService)
    now = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    service = AssistantAssessmentService(db_session, now=lambda: now)
    with pytest.raises(AssistantAssessmentError, match="paused"):
        service.run_now()
    service.configure(True)

    result = service.run_now()

    assert result == {"status": "queued", "session_id": 41, "turn_id": 73}
    assert calls == [
        ("init", "assistant"),
        ("create", "assessment"),
        ("enqueue", 41, ASSISTANT_ASSESSMENT_INSTRUCTION),
    ]
    assert service.status()["last_status"] == "queued"


def test_busy_oldest_session_blocks_occurrence_without_retry(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FullAssistantSessions:
        def __init__(self, db: Session, *, agent_id: str) -> None:
            pass

        def create_session(self, *, origin: str, commit: bool):
            raise AssistantSessionCapacityError(
                "Assistant session capacity is full because the oldest session is busy"
            )

    monkeypatch.setattr(
        assistant_assessment_service,
        "ActSessionService",
        FullAssistantSessions,
    )
    enabled_at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    due_at = enabled_at + timedelta(days=3)
    service = AssistantAssessmentService(db_session, now=lambda: due_at)
    service.configure(True)

    result = service.run_scheduled(due_at)

    assert result["status"] == "blocked"
    assert "capacity" in result["reason"]
    state = db_session.get(AssistantAssessmentState, 1)
    assert state is not None
    assert state.last_status == "blocked"
    assert state.next_run_at.replace(tzinfo=UTC) == due_at + timedelta(days=3)


def test_scheduler_registers_enabled_assessment_and_claims_latest_missed_once(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import scheduler_service

    monkeypatch.setattr(scheduler_service, "IntervalTrigger", None)
    enabled_at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    AssistantAssessmentService(db_session, now=lambda: enabled_at).configure(True)
    fake_scheduler = FakeScheduler()
    scheduler = SchedulerService(
        db_session,
        scheduler=fake_scheduler,
        session_factory=sessionmaker(bind=db_session.bind, autoflush=False, autocommit=False),
    )

    scheduler.register_assistant_assessment()

    job = fake_scheduler.jobs[ASSISTANT_ASSESSMENT_JOB_ID]
    assert job["trigger"] == {
        "type": "interval",
        "days": 3,
        "start_date": enabled_at + timedelta(days=3),
        "timezone": "UTC",
    }
    startup_at = enabled_at + timedelta(days=10)
    scheduler.queue_startup_catchups(startup_at)
    scheduler.queue_startup_catchups(startup_at)

    occurrences = db_session.scalars(
        select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_key == "platform:backend.assistant.assessment"
        )
    ).all()
    assert len(occurrences) == 1
    assert occurrences[0].scheduled_for_at.replace(tzinfo=UTC) == enabled_at + timedelta(
        days=9
    )
    assert occurrences[0].trigger_reason == "startup_catch_up"
    assert f"startup_schedule_occurrence_{occurrences[0].id}" in fake_scheduler.jobs


def test_claimed_assessment_queues_once_and_advances_the_persisted_interval(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeActSessionService:
        def __init__(self, db: Session, *, agent_id: str) -> None:
            assert agent_id == "assistant"

        def create_session(self, *, origin: str, commit: bool):
            assert origin == "assessment"
            assert commit is False
            return SimpleNamespace(id=8)

        def enqueue_turn(self, session_id: int, message: str, *, commit: bool):
            assert session_id == 8
            assert message == ASSISTANT_ASSESSMENT_INSTRUCTION
            assert commit is True
            return SimpleNamespace(id=13)

    monkeypatch.setattr(assistant_assessment_service, "ActSessionService", FakeActSessionService)
    enabled_at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    due_at = enabled_at + timedelta(days=3)
    AssistantAssessmentService(db_session, now=lambda: enabled_at).configure(True)
    scheduler = SchedulerService(db_session, scheduler=FakeScheduler())
    occurrence = scheduler._claim_occurrence(
        schedule_key=scheduler._assistant_assessment_schedule_key(),
        definition_fingerprint=scheduler._assistant_assessment_definition_fingerprint(),
        scheduled_for_at=due_at,
        trigger_reason="automatic",
    )
    assert occurrence is not None

    result = scheduler._execute_claimed_assistant_assessment(occurrence)

    assert result == {"status": "queued", "session_id": 8, "turn_id": 13}
    assert occurrence.status == "queued"
    state = db_session.get(AssistantAssessmentState, 1)
    assert state is not None
    assert state.next_run_at.replace(tzinfo=UTC) == due_at + timedelta(days=3)


def test_pause_and_resume_blocks_an_occurrence_from_the_previous_anchor(
    db_session: Session,
) -> None:
    enabled_at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    assessment = AssistantAssessmentService(db_session, now=lambda: enabled_at)
    assessment.configure(True)
    scheduler = SchedulerService(db_session, scheduler=FakeScheduler())
    occurrence = scheduler._claim_occurrence(
        schedule_key=scheduler._assistant_assessment_schedule_key(),
        definition_fingerprint=scheduler._assistant_assessment_definition_fingerprint(),
        scheduled_for_at=enabled_at + timedelta(days=3),
        trigger_reason="automatic",
    )
    assert occurrence is not None

    assessment.configure(False)
    AssistantAssessmentService(
        db_session,
        now=lambda: enabled_at + timedelta(days=1),
    ).configure(True)
    result = scheduler._execute_claimed_assistant_assessment(occurrence)

    assert result is not None
    assert result["status"] == "blocked"
    assert occurrence.status == "blocked"
    assert "changed before execution" in occurrence.error_message


def test_pausing_removes_assessment_job(db_session: Session) -> None:
    fake_scheduler = FakeScheduler()
    scheduler = SchedulerService(db_session, scheduler=fake_scheduler)

    scheduler.configure_assistant_assessment(True)
    assert ASSISTANT_ASSESSMENT_JOB_ID in fake_scheduler.jobs

    status = scheduler.configure_assistant_assessment(False)

    assert status["enabled"] is False
    assert ASSISTANT_ASSESSMENT_JOB_ID not in fake_scheduler.jobs


def test_assessment_is_visible_in_schedules_while_paused(db_session):
    from app.schemas.schedule import ScheduleRead

    row = SchedulerService(db_session, scheduler=FakeScheduler()).serialize_assistant_assessment_schedule()
    assert ScheduleRead.model_validate(row).status == "paused"
    assert row["service_id"] == "backend.assistant.assessment"
    assert row["schedule_json"]["every"] == 3


def test_completed_assessment_notifies_even_without_proposals(db_session, monkeypatch):
    from app.models import ActTurn
    from app.services.telegram_service import TelegramService

    sent = []
    monkeypatch.setattr(TelegramService, "execute_notification", lambda self, payload: sent.append(payload))
    AssistantAssessmentService(db_session).notify_completed(ActTurn(
        session_id=12, user_message="Assess", status="succeeded", assistant_message="No new work to propose."
    ))
    assert len(sent) == 1
    assert "0 proposals" in sent[0]["description"]
    assert sent[0] == {"title": "Assessment Success", "description": "You have 0 proposals."}


def test_assessment_selects_assistant_thread_for_reply(db_session, monkeypatch):
    from app.models import ActSession, ActTelegramBinding, ActTurn, TelegramBotConnection
    from app.services.telegram_service import TelegramService

    session = ActSession(agent_id="assistant", origin="assessment", codex_thread_id="assessment-thread")
    connection = TelegramBotConnection(
        role="assistant_agent", status="connected", secret_store_id="fake", secret_reference="fake",
        bot_id="42", paired_chat_id="100", paired_user_id="200",
    )
    db_session.add_all([session, connection])
    db_session.flush()
    turn = ActTurn(session_id=session.id, user_message="Assess", status="succeeded", assistant_message="Any context?")
    db_session.add(turn)
    db_session.commit()
    monkeypatch.setattr(TelegramService, "execute_notification", lambda *args: None)
    AssistantAssessmentService(db_session).notify_completed(turn)
    assert turn.delivery_status == "pending"
    assert turn.delivery_connection_id == connection.id
    assert turn.delivery_chat_id == "100"
    assert db_session.get(ActTelegramBinding, connection.id).active_session_id == session.id


@pytest.mark.parametrize("unavailable", [False, True])
def test_assessment_cleanup_checks_attachments_and_removes_only_stale_history(
    db_session, monkeypatch, tmp_path, unavailable,
):
    from app.models import AgentProposal
    from app.services.github_provider import IntegrationProviderError
    from app.services.integration_service import IntegrationError

    directory = tmp_path / "assistant" / "plans"
    directory.mkdir(parents=True)
    monkeypatch.setattr(
        assistant_assessment_service, "ensure_act_workspace", lambda: SimpleNamespace(knowledge=tmp_path)
    )
    references = ["todo:active", "todo:done", "todo:missing", "goal:active", "goal:done", "goal:missing", "legacy"]
    for number, reference in enumerate(references, 1):
        db_session.add(AgentProposal(
            id=number, source_session_id=1, title=reference, rationale="reason", actions="action",
            instruction="instruction", references_json=[reference], fingerprint=str(number),
        ))
        (directory / f"{number}.json").write_text("{}", encoding="utf-8")
    db_session.commit()

    def invoke(operation, payload):
        assert operation == "notion.todo.list"
        if unavailable:
            raise IntegrationError("provider_unavailable", "Unavailable")
        if "start_cursor" not in payload:
            return {"todos": [{"id": "done", "done": True}], "has_more": True, "next_cursor": "next"}
        assert payload["start_cursor"] == "next"
        return {"todos": [{"id": "active", "done": False}], "has_more": False, "next_cursor": None}

    monkeypatch.setattr(
        assistant_assessment_service, "build_default_integration_service",
        lambda db: SimpleNamespace(invoke_direct=invoke),
    )

    def request(self, path, payload, **kwargs):
        if unavailable:
            raise IntegrationProviderError("provider_unavailable", "Unavailable")
        if path == "/api/records?category=goal":
            return [{"id": name, "category": "goal", "title": name, "data": {}} for name in ["active", "done"]]
        return {"goal": {"progress": 100 if "/done/" in path else 50}}

    monkeypatch.setattr(assistant_assessment_service.UrllibAtlasProviderAdapter, "_request", request)
    AssistantAssessmentService(db_session).cleanup_proposals()
    expected = set(range(1, 8)) if unavailable else {1, 4, 7}
    assert set(db_session.scalars(select(AgentProposal.id))) == expected
    assert {int(path.stem) for path in directory.iterdir()} == expected
