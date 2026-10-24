"""Recurring responsibilities share durable agenda occurrence claims and recovery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import ActTurn, AssistantAgendaItem, AssistantState
from app.schemas.assistant import AgendaCreate, AgendaUpdate
from app.services.act_session_service import ActSessionService
from app.services.assistant_activation_service import AssistantActivationService
from app.services.assistant_service import AssistantService


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def daily(**extra):
    return {"frequency": "daily", "time": "09:00", "timezone": "America/Toronto", **extra}


def freeze(monkeypatch, value):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr("app.services.assistant_activation_service.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_service.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_recurrence.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_turn_dispatcher.datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.assistant_planning.datetime", FrozenDatetime)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'recurrence.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr("app.services.assistant_sources.collect_sources", lambda *_a, **_k: {})
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    with Session(engine) as session:
        service = AssistantService(session)
        service.state()
        for item in service.agenda():
            service.delete_item(item["id"], item["revision"])
        yield session
    engine.dispose()


def recurring(db, monkeypatch, *, title="Review progress", at="2026-10-16T12:00:00Z", **extra):
    freeze(monkeypatch, instant(at))
    return AssistantService(db).create_item(AgendaCreate(title=title, recurrence=daily(), **extra))


def complete(db, turn_id, status="succeeded"):
    turn = db.get(ActTurn, turn_id)
    turn.status = status
    turn.assistant_message = ""
    turn.completed_at = datetime.now(UTC)
    db.commit()
    AssistantActivationService(db).on_turn_completed(turn)
    return turn


def occurrence(db, occurrence_id):
    from app.models import AssistantAgendaOccurrence

    return db.get(AssistantAgendaOccurrence, occurrence_id)


@pytest.mark.parametrize("changes", [
    {"frequency": "hourly"}, {"time": "25:00"}, {"time": "9:00"},
    {"timezone": "Mars/Unknown"}, {"frequency": "weekly", "weekdays": []},
    {"frequency": "weekly", "weekdays": [7]},
    {"frequency": "monthly", "day_of_month": 0},
    {"frequency": "monthly", "day_of_month": 32},
])
def test_invalid_recurrence_rejected(changes):
    with pytest.raises(ValidationError):
        AgendaCreate(title="Invalid schedule", recurrence=daily(**changes))


def test_questions_cannot_recur_and_update_can_remove_recurrence(db, monkeypatch):
    with pytest.raises(ValidationError):
        AgendaCreate(kind="question", title="Why?", recurrence=daily())
    item = recurring(db, monkeypatch)
    with pytest.raises((ValidationError, ValueError)):
        AssistantService(db).update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], kind="question"))
    db.rollback()
    changed = AssistantService(db).update_item(item["id"], AgendaUpdate(
        expected_revision=item["revision"], recurrence=None))
    assert changed["recurrence"] is None


@pytest.mark.parametrize(("definition", "now", "latest", "next_due"), [
    (daily(), "2026-10-20T12:00:00Z", "2026-10-19T13:00:00Z", "2026-10-20T13:00:00Z"),
    (daily(), "2026-10-20T13:00:00Z", "2026-10-20T13:00:00Z", "2026-10-21T13:00:00Z"),
    (daily(frequency="weekly", weekdays=[0, 4]), "2026-10-20T15:00:00Z",
     "2026-10-19T13:00:00Z", "2026-10-23T13:00:00Z"),
    (daily(frequency="monthly", day_of_month=31), "2026-04-30T15:00:00Z",
     "2026-03-31T13:00:00Z", "2026-05-31T13:00:00Z"),
    (daily(frequency="monthly", day_of_month=29), "2028-02-29T15:00:00Z",
     "2028-02-29T14:00:00Z", "2028-03-29T13:00:00Z"),
    (daily(time="02:30"), "2026-03-08T07:00:00Z", "2026-03-07T07:30:00Z", "2026-03-08T07:30:00Z"),
    (daily(time="02:30"), "2026-03-08T07:30:00Z", "2026-03-08T07:30:00Z", "2026-03-09T06:30:00Z"),
    (daily(time="01:30"), "2026-11-01T06:45:00Z", "2026-11-01T05:30:00Z", "2026-11-02T06:30:00Z"),
])
def test_calendar_recurrence_uses_wall_clock_and_latest_only(definition, now, latest, next_due):
    from app.services.assistant_recurrence import latest_at, next_at

    assert latest_at(definition, instant(now)) == instant(latest)
    assert next_at(definition, instant(now)) == instant(next_due)


def test_new_task_does_not_recover_before_creation(db, monkeypatch):
    recurring(db, monkeypatch, at="2026-10-16T15:00:00Z")
    assert AssistantActivationService(db).tick()["status"] == "idle"
    freeze(monkeypatch, instant("2026-10-17T13:00:00Z"))
    assert AssistantActivationService(db).tick()["status"] == "queued"


def test_each_task_recovers_only_latest_missed_and_success_is_durable(db, monkeypatch):
    from app.models import AssistantAgendaOccurrence

    first = recurring(db, monkeypatch)
    second = recurring(db, monkeypatch, title="Evening plan")
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    service = AssistantActivationService(db)
    seen = set()
    for _ in range(2):
        result = service.tick()
        turn = db.get(ActTurn, result["turn_id"])
        activation = turn.assistant_context_json
        assert result["kind"] == activation["kind"] == "agenda"
        claimed = activation["occurrence"]
        assert instant(claimed["scheduled_at"]) == instant("2026-10-20T13:00:00Z")
        assert len(activation["agenda"]) == 1
        assert activation["agenda"][0]["id"] == claimed["agenda_item_id"]
        seen.add(claimed["agenda_item_id"])
        assert service.tick()["status"] == "busy"
        complete(db, turn.id)
        assert occurrence(db, claimed["id"]).status == "succeeded"
    assert seen == {first["id"], second["id"]}
    with Session(db.get_bind()) as restarted:
        assert AssistantActivationService(restarted).tick()["status"] == "idle"
    rows = list(db.scalars(select(AssistantAgendaOccurrence)))
    assert len(rows) == 2
    assert all(utc(row.scheduled_at) == instant("2026-10-20T13:00:00Z") for row in rows)


def test_concurrent_ticks_claim_one_occurrence(db, monkeypatch):
    from app.models import AssistantAgendaOccurrence

    recurring(db, monkeypatch)
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    factory = sessionmaker(bind=db.get_bind())

    def tick():
        with factory() as other:
            return AssistantActivationService(other).tick()["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tick(), range(2)))
    assert sorted(results) == ["busy", "queued"]
    assert len(list(db.scalars(select(AssistantAgendaOccurrence)))) == 1
    assert len(list(db.scalars(select(ActTurn)))) == 1


def test_failure_retries_same_occurrence_with_delay_and_prior_attempt_context(db, monkeypatch):
    item = recurring(db, monkeypatch)
    now = instant("2026-10-20T15:00:00Z")
    freeze(monkeypatch, now)
    service = AssistantActivationService(db)
    first = service.tick()
    turn = complete(db, first["turn_id"], "failed")
    claimed = turn.assistant_context_json["occurrence"]
    failed = occurrence(db, claimed["id"])
    assert failed.status == "failed"
    assert utc(failed.retry_at) >= now + timedelta(seconds=30)
    assert service.tick()["status"] == "idle"
    freeze(monkeypatch, now + timedelta(seconds=31))
    retried = service.tick()
    retry_turn = db.get(ActTurn, retried["turn_id"])
    assert retry_turn.assistant_context_json["occurrence"]["id"] == claimed["id"]
    assert retry_turn.assistant_context_json["occurrence"]["previous_turn_id"] == turn.id
    assert occurrence(db, claimed["id"]).attempts == 2
    assert retry_turn.user_message == (item["notes"] or item["title"])


def test_failed_older_occurrence_is_superseded_by_latest_missed(db, monkeypatch):
    from app.models import AssistantAgendaOccurrence

    recurring(db, monkeypatch)
    freeze(monkeypatch, instant("2026-10-16T15:00:00Z"))
    first = AssistantActivationService(db).tick()
    old = complete(db, first["turn_id"], "failed").assistant_context_json["occurrence"]
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    latest = AssistantActivationService(db).tick()
    claimed = db.get(ActTurn, latest["turn_id"]).assistant_context_json["occurrence"]
    assert claimed["id"] != old["id"]
    assert instant(claimed["scheduled_at"]) == instant("2026-10-20T13:00:00Z")
    assert occurrence(db, old["id"]).status == "skipped"
    assert len(list(db.scalars(select(AssistantAgendaOccurrence)))) == 2


@pytest.mark.parametrize("status", ["waiting", "done", "dismissed"])
def test_nonopen_recurring_task_does_not_fire(db, monkeypatch, status):
    item = recurring(db, monkeypatch)
    AssistantService(db).update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], status=status))
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    assert AssistantActivationService(db).tick()["status"] == "idle"


def test_resuming_paused_task_recovers_latest_only_then_continues_next_day(db, monkeypatch):
    item = recurring(db, monkeypatch)
    service = AssistantService(db)
    paused = service.update_item(item["id"], AgendaUpdate(expected_revision=item["revision"], status="waiting"))
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    service.update_item(item["id"], AgendaUpdate(expected_revision=paused["revision"], status="open"))
    first = AssistantActivationService(db).tick()
    first_turn = complete(db, first["turn_id"])
    assert instant(first_turn.assistant_context_json["occurrence"]["scheduled_at"]) == instant("2026-10-20T13:00:00Z")
    assert AssistantActivationService(db).tick()["status"] == "idle"
    freeze(monkeypatch, instant("2026-10-21T13:00:00Z"))
    next_result = AssistantActivationService(db).tick()
    next_turn = db.get(ActTurn, next_result["turn_id"])
    assert instant(next_turn.assistant_context_json["occurrence"]["scheduled_at"]) == instant("2026-10-21T13:00:00Z")


def test_disabled_assistant_preserves_recurring_task_until_reenabled(db, monkeypatch):
    recurring(db, monkeypatch)
    state = db.get(AssistantState, 1)
    state.enabled = False
    db.commit()
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    assert AssistantActivationService(db).tick()["status"] == "disabled"
    state.enabled = True
    db.commit()
    assert AssistantActivationService(db).tick()["status"] == "queued"


def test_schedule_edit_does_not_resurrect_failed_old_occurrence(db, monkeypatch):
    item = recurring(db, monkeypatch)
    freeze(monkeypatch, instant("2026-10-16T15:00:00Z"))
    first = AssistantActivationService(db).tick()
    old = complete(db, first["turn_id"], "failed").assistant_context_json["occurrence"]
    current = AssistantService(db).serialize_item(db.get(AssistantAgendaItem, item["id"]))
    AssistantService(db).update_item(item["id"], AgendaUpdate(
        expected_revision=current["revision"], recurrence=daily(time="17:00")))
    freeze(monkeypatch, instant("2026-10-16T15:01:00Z"))
    assert AssistantActivationService(db).tick()["status"] == "idle"
    freeze(monkeypatch, instant("2026-10-16T21:00:00Z"))
    result = AssistantActivationService(db).tick()
    latest = db.get(ActTurn, result["turn_id"]).assistant_context_json["occurrence"]
    assert latest["id"] != old["id"]
    assert instant(latest["scheduled_at"]) == instant("2026-10-16T21:00:00Z")


def test_deleted_failed_task_does_not_retry_or_reuse_occurrence_identity(db, monkeypatch):
    item = recurring(db, monkeypatch)
    freeze(monkeypatch, instant("2026-10-16T15:00:00Z"))
    result = AssistantActivationService(db).tick()
    complete(db, result["turn_id"], "failed")
    current = db.get(AssistantAgendaItem, item["id"])
    AssistantService(db).delete_item(item["id"], current.revision)
    freeze(monkeypatch, instant("2026-10-16T15:01:00Z"))
    assert AssistantActivationService(db).tick()["status"] == "idle"
    replacement = AssistantService(db).create_item(AgendaCreate(title="Replacement", recurrence=daily()))
    assert replacement["id"] != item["id"]


def test_manual_run_uses_normal_durable_occurrence_and_revision(db, monkeypatch):
    item = recurring(db, monkeypatch)
    with pytest.raises(ValueError, match="changed|revision"):
        AssistantActivationService(db).run_now(item["id"], item["revision"] + 1)
    db.rollback()
    result = AssistantActivationService(db).run_now(item["id"], item["revision"])
    turn = db.get(ActTurn, result["turn_id"])
    assert result["kind"] == "agenda"
    claimed = turn.assistant_context_json["occurrence"]
    assert claimed["agenda_item_id"] == item["id"]
    assert occurrence(db, claimed["id"]).turn_id == turn.id
    complete(db, turn.id)
    assert occurrence(db, claimed["id"]).status == "succeeded"


def test_failed_manual_occurrence_retries_until_superseded_by_calendar_due(db, monkeypatch):
    item = recurring(db, monkeypatch)
    first = AssistantActivationService(db).run_now(item["id"], item["revision"])
    first_turn = complete(db, first["turn_id"], "failed")
    manual = first_turn.assistant_context_json["occurrence"]
    assert instant(manual["scheduled_at"]) == instant("2026-10-16T12:00:00Z")
    assert AssistantActivationService(db).tick()["status"] == "idle"
    freeze(monkeypatch, instant("2026-10-16T12:00:31Z"))
    retry = AssistantActivationService(db).tick()
    retried = db.get(ActTurn, retry["turn_id"])
    context = retried.assistant_context_json["occurrence"]
    assert context["id"] == manual["id"]
    assert context["attempts"] == 2 and context["previous_turn_id"] == first_turn.id
    complete(db, retried.id, "failed")
    freeze(monkeypatch, instant("2026-10-16T13:00:00Z"))
    calendar = AssistantActivationService(db).tick()
    latest = db.get(ActTurn, calendar["turn_id"]).assistant_context_json["occurrence"]
    assert latest["id"] != manual["id"]
    assert instant(latest["scheduled_at"]) == instant("2026-10-16T13:00:00Z")
    assert occurrence(db, manual["id"]).status == "skipped"


def test_saving_migrated_recurrence_unchanged_preserves_schedule_identity(db, monkeypatch):
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    state = db.get(AssistantState, 1)
    state.agenda_schedules_migrated = False
    db.commit()
    service = AssistantService(db)
    service.state()
    migrated = next(item for item in service.agenda() if item["title"] == "Morning planning")
    row = db.get(AssistantAgendaItem, migrated["id"])
    start, version = utc(row.recurrence_start_at), row.schedule_version
    freeze(monkeypatch, instant("2026-10-21T15:00:00Z"))
    changed = service.update_item(row.id, AgendaUpdate(expected_revision=migrated["revision"],
        recurrence={**migrated["recurrence"], "weekdays": None, "day_of_month": None}))
    db.refresh(row)
    assert row.schedule_version == version
    assert utc(row.recurrence_start_at) == start
    assert changed["recurrence"] == migrated["recurrence"]
    result = AssistantActivationService(db).tick()
    assert result["status"] == "queued"
    assert datetime.fromisoformat(db.get(ActTurn, result["turn_id"]).assistant_context_json[
        "occurrence"]["scheduled_at"]) <= instant("2026-10-21T15:00:00Z")


@pytest.mark.parametrize("change", ["pause", "delete", "reschedule", "disable"])
def test_queued_recurring_task_changed_before_execution_cannot_run(db, monkeypatch, change):
    from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher

    item = recurring(db, monkeypatch)
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    result = AssistantActivationService(db).tick()
    turn = db.get(ActTurn, result["turn_id"])
    service = AssistantService(db)
    current = db.get(AssistantAgendaItem, item["id"])
    if change == "pause":
        service.update_item(item["id"], AgendaUpdate(expected_revision=current.revision, status="waiting"))
    elif change == "delete":
        service.delete_item(item["id"], current.revision)
    elif change == "reschedule":
        service.update_item(item["id"], AgendaUpdate(expected_revision=current.revision, recurrence=daily(time="17:00")))
    else:
        db.get(AssistantState, 1).enabled = False
        db.commit()
    with pytest.raises(ValueError, match="paused|ended|deleted|rescheduled"):
        AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)


def test_initial_tasks_are_ordinary_editable_agenda_and_seeded_exactly_once(tmp_path, monkeypatch):
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    engine = create_engine(f"sqlite:///{tmp_path / 'seeding.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            service = AssistantService(session)
            service.state()
            initial = service.agenda()
            assert {item["title"] for item in initial} == {"Morning planning", "Evening reflection", "Daily check-in"}
            assert all(item["kind"] == "task" and item["recurrence"]["frequency"] == "daily" for item in initial)
            morning = next(item for item in initial if item["title"] == "Morning planning")
            reflection = next(item for item in initial if item["title"] == "Evening reflection")
            service.update_item(morning["id"], AgendaUpdate(expected_revision=morning["revision"],
                title="My morning planning", recurrence=daily(time="11:15"), status="waiting"))
            service.delete_item(reflection["id"], reflection["revision"])
        with Session(engine) as restarted:
            service = AssistantService(restarted)
            service.state()
            tasks = service.agenda()
            assert len(tasks) == 2
            morning = next(item for item in tasks if item["title"] == "My morning planning")
            assert morning["status"] == "waiting"
            assert morning["recurrence"]["time"] == "11:15"
    finally:
        engine.dispose()


def test_legacy_settings_and_success_receipts_migrate_without_duplicate_completion(tmp_path, monkeypatch):
    freeze(monkeypatch, instant("2026-10-20T23:00:00Z"))
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    try:
        with Session(engine) as session:
            session.add(AssistantState(id=1, daily_time="08:15", reflection_time="18:30", timezone="UTC",
                last_daily_at=instant("2026-10-20T08:15:00Z"),
                last_reflection_at=instant("2026-10-20T18:30:00Z")))
            session.commit()
            service = AssistantService(session)
            service.state()
            tasks = {item["title"]: item for item in service.agenda()}
            assert tasks["Morning planning"]["recurrence"]["time"] == "08:15"
            assert tasks["Evening reflection"]["recurrence"]["time"] == "18:30"
            assert all(item["recurrence"]["timezone"] == "UTC" for item in tasks.values())
            assert all(tasks[title]["last_occurrence"]["status"] == "succeeded"
                       for title in ("Morning planning", "Evening reflection"))
            assert tasks["Daily check-in"]["last_occurrence"] is None
            assert AssistantActivationService(session).tick()["status"] == "idle"
        with Session(engine) as restarted:
            assert len(AssistantService(restarted).agenda()) == 3
            assert AssistantActivationService(restarted).tick()["status"] == "idle"
    finally:
        engine.dispose()


def test_failed_manual_occurrence_retries_with_same_identity_and_prior_turn(db, monkeypatch):
    item = recurring(db, monkeypatch)
    service = AssistantActivationService(db)
    first = service.run_now(item["id"], item["revision"])
    old_turn = complete(db, first["turn_id"], "failed")
    claimed = old_turn.assistant_context_json["occurrence"]
    assert service.tick()["status"] == "idle"
    freeze(monkeypatch, instant("2026-10-16T12:00:31Z"))
    retried = service.tick()
    turn = db.get(ActTurn, retried["turn_id"])
    assert turn.assistant_context_json["occurrence"]["id"] == claimed["id"]
    assert turn.assistant_context_json["occurrence"]["previous_turn_id"] == old_turn.id
    assert occurrence(db, claimed["id"]).attempts == 2
    complete(db, turn.id)
    freeze(monkeypatch, instant("2026-10-16T13:00:00Z"))
    scheduled = service.tick()
    assert db.get(ActTurn, scheduled["turn_id"]).assistant_context_json["occurrence"]["id"] != claimed["id"]


def test_explicit_manual_retry_retains_failed_attempt_before_delay(db, monkeypatch):
    item = recurring(db, monkeypatch)
    service = AssistantActivationService(db)
    first = service.run_now(item["id"], item["revision"])
    old = complete(db, first["turn_id"], "failed")
    freeze(monkeypatch, instant("2026-10-16T12:00:01Z"))
    retry = service.run_now(item["id"], item["revision"])
    turn = db.get(ActTurn, retry["turn_id"])
    assert turn.assistant_context_json["occurrence"]["id"] == old.assistant_context_json["occurrence"]["id"]


def test_outdated_queued_occurrence_is_skipped_before_model_execution(db, monkeypatch):
    from app.services.assistant_turn_dispatcher import AssistantTurnDispatcher

    recurring(db, monkeypatch)
    freeze(monkeypatch, instant("2026-10-16T15:00:00Z"))
    service = AssistantActivationService(db)
    first = service.tick()
    turn = db.get(ActTurn, first["turn_id"])
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    with pytest.raises(ValueError, match="superseded"):
        AssistantTurnDispatcher._turn_input(db, turn, recovered_thread=False)
    complete(db, turn.id, "failed")
    result = service.tick()
    current = db.get(ActTurn, result["turn_id"]).assistant_context_json["occurrence"]
    assert instant(current["scheduled_at"]) == instant("2026-10-20T13:00:00Z")
    assert occurrence(db, turn.assistant_context_json["occurrence"]["id"]).status == "skipped"


def test_repeated_task_creation_reuses_definition_after_calendar_advances(db, monkeypatch):
    item = recurring(db, monkeypatch)
    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    again = AssistantService(db).create_item(AgendaCreate(title=item["title"], recurrence=daily()))
    assert again["id"] == item["id"]
    one_time = AssistantService(db).create_item(AgendaCreate(title="Check once"))
    assert AssistantService(db).create_item(AgendaCreate(title="Check once"))["id"] == one_time["id"]


@pytest.mark.parametrize("legacy_status,old_day", [("queued", False), ("failed", False), ("queued", True)])
def test_legacy_queue_claims_are_not_imported_as_success_or_replayed_as_backlog(tmp_path, monkeypatch,
                                                                            legacy_status, old_day):
    from app.models import ActSession, AssistantAgendaOccurrence

    freeze(monkeypatch, instant("2026-10-20T15:00:00Z"))
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-claims.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ActSessionService, "synchronize_telegram", lambda *_: None)
    monkeypatch.setattr("app.services.assistant_initialization.AssistantInitializationService.ready", lambda *_: False)
    try:
        with Session(engine) as session:
            at = instant("2026-10-16T13:00:00Z" if old_day else "2026-10-20T13:00:00Z")
            session.add(AssistantState(id=1, last_daily_at=at, last_reflection_at=instant("2026-10-20T02:00:00Z")))
            chat = ActSession(agent_id="assistant", codex_thread_id="legacy")
            session.add(chat)
            session.flush()
            old = ActTurn(session_id=chat.id, status=legacy_status, user_message="Legacy review",
                          assistant_context_json={"kind": "daily", "at": at.isoformat(), "agenda": []})
            session.add(old)
            session.commit()
            tasks = AssistantService(session).agenda()
            morning = next(item for item in tasks if item["title"] == "Morning planning")
            rows = list(session.scalars(select(AssistantAgendaOccurrence).where(
                AssistantAgendaOccurrence.agenda_item_id == morning["id"])))
            assert not any(row.status == "succeeded" for row in rows)
            if legacy_status == "queued" and not old_day:
                assert old.assistant_context_json["kind"] == "agenda"
                assert rows[0].turn_id == old.id and rows[0].status == "queued"
            else:
                result = AssistantActivationService(session).tick()
                turn = session.get(ActTurn, result["turn_id"])
                assert instant(turn.assistant_context_json["occurrence"]["scheduled_at"]) == instant("2026-10-20T13:00:00Z")
                if old_day:
                    assert old.status == "cancelled"
    finally:
        engine.dispose()


def test_existing_schema_upgrades_idempotently_and_preserves_one_time_agenda(db, monkeypatch):
    from sqlalchemy import inspect, text

    from app import db as db_module

    item = AssistantService(db).create_item(AgendaCreate(title="Preserved task", notes="Original evidence"))
    with db.bind.begin() as connection:
        for column in ("recurrence_json", "recurrence_start_at", "schedule_version", "completion_requirement"):
            connection.execute(text(f"ALTER TABLE assistant_agenda DROP COLUMN {column}"))
        connection.execute(text("ALTER TABLE assistant_state DROP COLUMN agenda_schedules_migrated"))
        connection.execute(text("ALTER TABLE assistant_state DROP COLUMN planning_contract_migrated"))
    monkeypatch.setattr(db_module, "engine", db.bind)
    db_module.ensure_local_schema()
    db_module.ensure_local_schema()
    db.expire_all()
    assert {"recurrence_json", "recurrence_start_at", "schedule_version", "completion_requirement"}.issubset(
        {column["name"] for column in inspect(db.bind).get_columns("assistant_agenda")})
    row = db.get(AssistantAgendaItem, item["id"])
    assert row.title == "Preserved task" and row.notes == "Original evidence" and row.recurrence_json is None
