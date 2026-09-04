import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import ApprovalRequest, ScheduleOccurrence, ScheduleRuntimeState, Skill, SkillRun, SkillVersion
from app.routers.schedules import list_schedules
from app.schemas.schedule import SchedulePayload, ScheduleUpdate
from app.services.permission_service import PermissionService
from app.services.platform_service import PLATFORM_SCHEDULE_BY_ID, QUERCUS_SYNC_SERVICE_ID
from app.services.scheduler_service import (
    NOTION_DONE_CLEANUP_JOB_ID,
    NOTION_DONE_CLEANUP_SERVICE_ID,
    NOTION_DONE_CLEANUP_TIMEZONE,
    ScheduleError,
    SchedulerService,
)


class FakeJob:
    next_run_time = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)


class FakeScheduler:
    def __init__(self) -> None:
        self.running = False
        self.jobs: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []

    def start(self) -> None:
        self.running = True

    def shutdown(self, wait: bool = False) -> None:
        self.running = False

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
    ):
        self.jobs[id] = {
            "func": func,
            "trigger": trigger,
            "args": args,
            "replace_existing": replace_existing,
            "max_instances": max_instances,
            "coalesce": coalesce,
            "misfire_grace_time": misfire_grace_time,
        }
        return FakeJob()

    def remove_job(self, id: str) -> None:
        self.removed.append(id)
        self.jobs.pop(id, None)

    def get_job(self, id: str):
        if id not in self.jobs:
            return None
        return SimpleNamespace(next_run_time=FakeJob.next_run_time)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def manifest(name: str, runtime: str = "service", schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "name": name,
        "description": "Scheduled test service",
        "runtime": runtime,
        "entrypoint": "skill.py" if runtime != "web_app" else "app:app",
        "instructions_path": None,
        "input_schema": {"type": "object", "properties": {"hello": {"type": "string"}}, "additionalProperties": False} if runtime != "web_app" else None,
        "output_schema": {"type": "object", "additionalProperties": True} if runtime != "web_app" else None,
        "function_requirements": [],
        "integration_requirements": [],
        "dependencies": [],
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": schedule if runtime == "service" else None,
    }


def create_skill(
    db: Session,
    project_root: Path,
    name: str = "scheduled_service",
    *,
    runtime: str = "service",
    schedule: dict[str, Any] | None = None,
) -> Skill:
    schedule = schedule or {
        "type": "daily",
        "time": "08:00",
        "timezone": "America/Toronto",
        "input": {"hello": "world"},
    }
    skill_dir = project_root / "skills" / "installed" / name / "versions" / "v1"
    skill_dir.mkdir(parents=True)
    (skill_dir / "tests").mkdir()
    manifest_json = manifest(name, runtime, schedule)
    (skill_dir / "manifest.json").write_text(json.dumps(manifest_json), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    if runtime == "web_app":
        (skill_dir / "app.py").write_text("app = object()\n", encoding="utf-8")
    (skill_dir / "tests" / "test_skill.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    skill = Skill(
        name=name,
        description="Scheduled test service",
        runtime=runtime,
        status="installed",
        risk_level="low",
        manifest_path=(skill_dir / "manifest.json").relative_to(project_root).as_posix(),
        installed_path=skill_dir.relative_to(project_root).as_posix(),
        input_schema_json=manifest_json["input_schema"],
        output_schema_json=manifest_json["output_schema"],
        enabled=True,
    )
    db.add(skill)
    db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version="v1",
        status="active",
        folder_path=skill.installed_path,
        code_snapshot_path=skill.installed_path,
        manifest_json=manifest_json,
        permission_fingerprint="test",
        validation_status="passed",
        test_status="passed",
    )
    db.add(version)
    db.flush()
    skill.active_version_id = version.id
    db.commit()
    db.refresh(skill)
    return skill


def service(db: Session, project_root: Path, fake_scheduler: FakeScheduler | None = None) -> SchedulerService:
    return SchedulerService(
        db,
        scheduler=fake_scheduler or FakeScheduler(),
        project_root=project_root,
        session_factory=sessionmaker(bind=db.bind, autoflush=False, autocommit=False),
    )


def test_start_registers_backend_owned_platform_services(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import scheduler_service

    monkeypatch.setattr(scheduler_service, "CronTrigger", None)
    fake_scheduler = FakeScheduler()
    scheduler = service(db_session, tmp_path, fake_scheduler)

    scheduler.start()

    job = fake_scheduler.jobs[NOTION_DONE_CLEANUP_JOB_ID]
    assert job["args"] == [NOTION_DONE_CLEANUP_SERVICE_ID]
    assert job["trigger"]["timezone"] == NOTION_DONE_CLEANUP_TIMEZONE
    assert job["max_instances"] == 1
    assert job["coalesce"] is True
    quercus_definition = PLATFORM_SCHEDULE_BY_ID[QUERCUS_SYNC_SERVICE_ID]
    quercus_job = fake_scheduler.jobs[quercus_definition.job_id]
    assert quercus_job["args"] == [QUERCUS_SYNC_SERVICE_ID]
    assert quercus_job["trigger"]["hour"] == 10
    assert quercus_job["trigger"]["timezone"] == "America/Toronto"
    assert quercus_job["max_instances"] == 1
    assert quercus_job["coalesce"] is True
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(scheduler_service=scheduler)))
    listed = list_schedules(request, skill_id=None, db=db_session)  # type: ignore[arg-type]
    assert listed[0]["schedule_kind"] == "platform"
    assert listed[0]["service_id"] == NOTION_DONE_CLEANUP_SERVICE_ID
    assert listed[0]["read_only"] is True
    assert [item["service_id"] for item in listed[:2]] == [
        NOTION_DONE_CLEANUP_SERVICE_ID,
        QUERCUS_SYNC_SERVICE_ID,
    ]


def test_quercus_platform_schedule_follows_toronto_dst(tmp_path: Path, db_session: Session) -> None:
    scheduler = service(db_session, tmp_path)
    definition = PLATFORM_SCHEDULE_BY_ID[QUERCUS_SYNC_SERVICE_ID]
    state = scheduler._ensure_platform_state(QUERCUS_SYNC_SERVICE_ID, datetime(2025, 1, 1, tzinfo=UTC))

    winter = scheduler._latest_platform_due_at(definition, state, datetime(2026, 1, 15, 15, 5, tzinfo=UTC))
    summer = scheduler._latest_platform_due_at(definition, state, datetime(2026, 7, 15, 14, 5, tzinfo=UTC))

    assert winter == datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
    assert summer == datetime(2026, 7, 15, 14, 0, tzinfo=UTC)


def test_manifest_creates_exactly_one_paused_service_schedule(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)

    schedule = scheduler.create_from_manifest(skill)

    assert schedule.status == "paused"
    assert schedule.input_json == {"hello": "world"}
    assert schedule.schedule_type == "daily"
    with pytest.raises(ScheduleError, match="already has"):
        scheduler.create_from_manifest(skill)


@pytest.mark.parametrize("runtime", ["function", "web_app"])
def test_functions_and_web_apps_cannot_be_scheduled(
    tmp_path: Path,
    db_session: Session,
    runtime: str,
) -> None:
    skill = create_skill(db_session, tmp_path, f"not_{runtime}", runtime=runtime)

    with pytest.raises(ScheduleError, match="Only service skills"):
        service(db_session, tmp_path).create_from_manifest(skill)


def test_service_schedule_input_must_match_manifest_schema(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(
        db_session,
        tmp_path,
        schedule={
            "type": "daily",
            "time": "08:00",
            "timezone": "America/Toronto",
            "input": {"unexpected": True},
        },
    )

    with pytest.raises(ScheduleError, match="does not match its schema"):
        service(db_session, tmp_path).create_from_manifest(skill)


def test_enable_disable_and_edit_manage_one_service_job(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_skill(db_session, tmp_path)
    fake_scheduler = FakeScheduler()
    scheduler = service(db_session, tmp_path, fake_scheduler)
    schedule = scheduler.create_from_manifest(skill)
    db_session.commit()
    monkeypatch.setattr(scheduler, "_validate_service_ready", lambda *_args: None)

    enabled = scheduler.enable_service(schedule)
    assert enabled.status == "active"
    assert skill.enabled is True
    assert scheduler.job_id(schedule.id) in fake_scheduler.jobs

    updated = scheduler.update_schedule(
        schedule,
        ScheduleUpdate(
            name="Weekly cleanup",
            schedule=SchedulePayload(
                type="weekly",
                day="friday",
                time="09:30",
                timezone="America/Toronto",
                input={"hello": "updated"},
            ),
        ),
    )
    assert updated.status == "active"
    assert updated.schedule_json["day"] == "friday"
    assert scheduler.job_id(schedule.id) in fake_scheduler.jobs

    disabled = scheduler.disable_service(schedule)
    assert disabled.status == "paused"
    assert skill.enabled is False
    assert scheduler.job_id(schedule.id) not in fake_scheduler.jobs


def test_enable_refreshes_stale_runtime_permission_request(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    db_session.commit()
    permission_service = PermissionService(db_session, project_root=tmp_path)
    approved = permission_service.create_runtime_request(skill)
    permission_service.approve_request(approved)

    manifest_path = tmp_path / skill.manifest_path
    manifest_json = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_json["permissions"]["codex"] = {
        "call_response": True,
        "internet_access": False,
    }
    manifest_path.write_text(json.dumps(manifest_json), encoding="utf-8")

    with pytest.raises(ScheduleError, match="Review the new runtime permission request"):
        scheduler.enable_service(schedule)

    requests = db_session.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.skill_id == skill.id,
            ApprovalRequest.request_scope == "runtime",
        )
        .order_by(ApprovalRequest.id)
    ).all()
    assert [request.status for request in requests] == ["superseded", "pending"]
    assert requests[-1].requested_permissions_json["codex"]["call_response"] is True
    assert skill.enabled is False


def test_run_now_requires_enabled_service(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    db_session.commit()

    def fake_run(
        _skill: Skill,
        input_json: dict[str, Any],
        schedule_id: int,
        **_kwargs: Any,
    ) -> SkillRun:
        run = SkillRun(
            skill_id=skill.id,
            version_id=skill.active_version_id,
            status="succeeded",
            input_json=input_json,
            output_json={"ok": True},
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            invocation_source="schedule",
            source_schedule_id=schedule_id,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        return run

    monkeypatch.setattr(scheduler, "_run_service_with_checks", fake_run)
    with pytest.raises(ScheduleError, match="disabled"):
        scheduler.run_scheduled_service(schedule)

    monkeypatch.setattr(scheduler, "_validate_service_ready", lambda *_args: None)
    scheduler.enable_service(schedule)
    run = scheduler.run_scheduled_service(schedule)

    assert schedule.status == "active"
    assert skill.enabled is True
    assert run.status == "succeeded"
    assert run.source_schedule_id == schedule.id
    assert schedule.last_run_status == "succeeded"


def test_automatic_execution_ignores_paused_schedule(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    db_session.commit()

    assert scheduler.execute_schedule(schedule.id) is None


def test_serialize_generated_schedule_uses_service_identity(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    scheduler.create_from_manifest(skill)
    db_session.commit()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(scheduler_service=scheduler)))

    listed = list_schedules(request, skill_id=skill.id, db=db_session)  # type: ignore[arg-type]

    assert len(listed) == 1
    assert listed[0]["schedule_kind"] == "service"
    assert listed[0]["service_id"] == skill.name
    assert listed[0]["read_only"] is False


def test_startup_claims_only_latest_missed_occurrence_once(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session, tmp_path)
    fake_scheduler = FakeScheduler()
    scheduler = service(db_session, tmp_path, fake_scheduler)
    schedule = scheduler.create_from_manifest(skill)
    schedule.status = "active"
    skill.enabled = True
    scheduler._set_schedule_state(
        schedule,
        active_since_at=datetime(2026, 8, 28, 10, 0, tzinfo=UTC),
    )
    db_session.commit()
    startup_at = datetime(2026, 8, 30, 13, 0, tzinfo=UTC)

    scheduler.queue_startup_catchups(startup_at)
    scheduler.queue_startup_catchups(startup_at)

    occurrences = db_session.scalars(select(ScheduleOccurrence)).all()
    assert len(occurrences) == 1
    assert occurrences[0].scheduled_for_at.replace(tzinfo=UTC) == datetime(
        2026,
        8,
        30,
        12,
        0,
        tzinfo=UTC,
    )
    assert occurrences[0].trigger_reason == "startup_catch_up"
    assert occurrences[0].occurrence_key
    assert f"startup_schedule_occurrence_{occurrences[0].id}" in fake_scheduler.jobs


def test_failed_occurrence_is_not_retried_but_next_occurrence_can_run(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    schedule.status = "active"
    skill.enabled = True
    scheduler._set_schedule_state(
        schedule,
        active_since_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
    )
    db_session.commit()

    scheduler.queue_startup_catchups(datetime(2026, 8, 30, 13, 0, tzinfo=UTC))
    first = db_session.scalar(select(ScheduleOccurrence))
    assert first is not None
    first.status = "failed"
    first.ended_at = datetime(2026, 8, 30, 13, 1, tzinfo=UTC)
    db_session.commit()

    scheduler.queue_startup_catchups(datetime(2026, 8, 30, 14, 0, tzinfo=UTC))
    schedule_key = f"skill:{schedule.id}"
    assert len(
        db_session.scalars(
            select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_key == schedule_key)
        ).all()
    ) == 1

    scheduler.queue_startup_catchups(datetime(2026, 8, 31, 13, 0, tzinfo=UTC))
    occurrences = db_session.scalars(
        select(ScheduleOccurrence)
        .where(ScheduleOccurrence.schedule_key == schedule_key)
        .order_by(ScheduleOccurrence.scheduled_for_at)
    ).all()
    assert len(occurrences) == 2
    assert occurrences[1].scheduled_for_at.replace(tzinfo=UTC) == datetime(
        2026,
        8,
        31,
        12,
        0,
        tzinfo=UTC,
    )


def test_automatic_callback_executes_one_claim_for_the_intended_time(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import scheduler_service

    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    schedule.status = "active"
    skill.enabled = True
    scheduler._set_schedule_state(
        schedule,
        active_since_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
    )
    db_session.commit()
    due_check_at = datetime(2026, 8, 30, 12, 0, 5, tzinfo=UTC)
    monkeypatch.setattr(scheduler_service, "utc_now", lambda: due_check_at)

    def fake_run(
        runtime: SchedulerService,
        target_schedule,
        **kwargs: Any,
    ) -> SkillRun:
        run = SkillRun(
            skill_id=target_schedule.skill_id,
            version_id=target_schedule.skill.active_version_id,
            status="succeeded",
            input_json=target_schedule.input_json,
            output_json={"ok": True},
            started_at=due_check_at,
            ended_at=due_check_at,
            invocation_source="schedule",
            source_schedule_id=target_schedule.id,
            schedule_occurrence_key=kwargs["schedule_occurrence_key"],
            scheduled_for_at=kwargs["scheduled_for_at"],
            schedule_trigger=kwargs["schedule_trigger"],
        )
        runtime.db.add(run)
        runtime.db.commit()
        runtime.db.refresh(run)
        target_schedule.last_run_at = run.ended_at
        target_schedule.last_run_status = run.status
        runtime.db.commit()
        return run

    monkeypatch.setattr(SchedulerService, "run_scheduled_service", fake_run)

    first = scheduler.execute_schedule(schedule.id)
    second = scheduler.execute_schedule(schedule.id)

    assert first is not None
    assert second is None
    assert first.schedule_occurrence_key
    assert first.scheduled_for_at.replace(tzinfo=UTC) == datetime(
        2026,
        8,
        30,
        12,
        0,
        tzinfo=UTC,
    )
    assert first.schedule_trigger == "automatic"
    assert len(db_session.scalars(select(ScheduleOccurrence)).all()) == 1


def test_interrupted_claim_is_failed_and_never_retried(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    schedule.status = "active"
    skill.enabled = True
    scheduler._set_schedule_state(
        schedule,
        active_since_at=datetime(2026, 8, 30, 10, 0, tzinfo=UTC),
    )
    db_session.commit()
    startup_at = datetime(2026, 8, 30, 13, 0, tzinfo=UTC)
    scheduler.queue_startup_catchups(startup_at)

    scheduler.recover_interrupted_occurrences(startup_at + timedelta(minutes=1))
    scheduler.queue_startup_catchups(startup_at + timedelta(minutes=2))

    occurrences = db_session.scalars(select(ScheduleOccurrence)).all()
    assert len(occurrences) == 1
    assert occurrences[0].status == "failed"
    assert "will not be retried" in occurrences[0].error_message


def test_interval_anchor_remains_stable_across_scheduler_instances(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(
        db_session,
        tmp_path,
        schedule={
            "type": "interval",
            "every": 5,
            "unit": "minutes",
            "timezone": "UTC",
            "input": {"hello": "world"},
        },
    )
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    schedule.status = "active"
    skill.enabled = True
    activated_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    scheduler._set_schedule_state(schedule, active_since_at=activated_at)
    db_session.commit()

    state = db_session.get(ScheduleRuntimeState, f"skill:{schedule.id}")
    assert state is not None
    assert state.interval_anchor_at.replace(tzinfo=UTC) == activated_at + timedelta(minutes=5)

    rebuilt = service(db_session, tmp_path).build_trigger(schedule)
    assert rebuilt.start_date == activated_at + timedelta(minutes=5)


def test_platform_schedule_uses_durable_once_only_occurrences(
    tmp_path: Path,
    db_session: Session,
) -> None:
    scheduler = service(db_session, tmp_path)
    first_start = datetime(2026, 8, 30, 13, 0, tzinfo=UTC)
    scheduler.register_platform_services(now=first_start)

    scheduler.queue_startup_catchups(first_start)
    assert db_session.scalar(select(ScheduleOccurrence)) is None

    later_start = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    scheduler.queue_startup_catchups(later_start)
    occurrence = db_session.scalar(
        select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_key
            == f"platform:{NOTION_DONE_CLEANUP_SERVICE_ID}"
        )
    )
    assert occurrence is not None
    occurrence.status = "failed"
    occurrence.started_at = later_start
    occurrence.ended_at = later_start
    db_session.commit()

    scheduler.queue_startup_catchups(later_start + timedelta(hours=1))
    occurrences = db_session.scalars(
        select(ScheduleOccurrence).where(
            ScheduleOccurrence.schedule_key
            == f"platform:{NOTION_DONE_CLEANUP_SERVICE_ID}"
        )
    ).all()
    assert len(occurrences) == 1

    listed = service(db_session, tmp_path).serialize_notion_done_cleanup_schedule()
    assert listed["last_run_status"] == "failed"
    assert listed["last_run_at"].replace(tzinfo=UTC) == later_start


def test_paused_schedule_never_claims_a_startup_occurrence(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    scheduler.create_from_manifest(skill)
    db_session.commit()

    scheduler.queue_startup_catchups(datetime(2026, 8, 31, 13, 0, tzinfo=UTC))

    assert db_session.scalar(
        select(ScheduleOccurrence).where(ScheduleOccurrence.schedule_key.like("skill:%"))
    ) is None
