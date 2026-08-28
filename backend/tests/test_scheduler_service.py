import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill, SkillRun, SkillVersion
from app.routers.schedules import list_schedules
from app.schemas.schedule import SchedulePayload, ScheduleUpdate
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

    def add_job(self, func, trigger, id: str, args: list[Any], replace_existing: bool, max_instances: int, coalesce: bool):
        self.jobs[id] = {
            "func": func,
            "trigger": trigger,
            "args": args,
            "replace_existing": replace_existing,
            "max_instances": max_instances,
            "coalesce": coalesce,
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


def test_start_registers_backend_owned_notion_service(
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
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(scheduler_service=scheduler)))
    listed = list_schedules(request, skill_id=None, db=db_session)  # type: ignore[arg-type]
    assert listed[0]["schedule_kind"] == "platform"
    assert listed[0]["service_id"] == NOTION_DONE_CLEANUP_SERVICE_ID
    assert listed[0]["read_only"] is True


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


def test_pause_resume_and_edit_manage_one_service_job(
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

    resumed = scheduler.resume_schedule(schedule)
    assert resumed.status == "active"
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

    paused = scheduler.pause_schedule(schedule)
    assert paused.status == "paused"
    assert scheduler.job_id(schedule.id) not in fake_scheduler.jobs


def test_run_now_works_while_service_schedule_is_paused(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule = scheduler.create_from_manifest(skill)
    db_session.commit()

    def fake_run(_skill: Skill, input_json: dict[str, Any], schedule_id: int) -> SkillRun:
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
    run = scheduler.run_scheduled_service(schedule)

    assert schedule.status == "paused"
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
