import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill, SkillSchedule
from app.routers.permission_requests import approve_permission_request
from app.routers.schedules import run_schedule_now
from app.schemas.schedule import ScheduleCreate, SchedulePayload
from app.services.permission_service import PermissionService
from app.services.scheduler_service import ScheduleError, SchedulerService


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


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def write_installed_skill(
    project_root: Path,
    name: str,
    *,
    enabled: bool = True,
    permissions: dict[str, Any] | None = None,
    schedule: dict[str, Any] | None = None,
) -> Path:
    skill_dir = project_root / "skills" / "installed" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "tests").mkdir()
    manifest = {
        "name": name,
        "description": "Scheduled test skill",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "risk_level": "low",
        "permissions": permissions
        or {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": schedule,
        "created_by": "codex",
        "enabled": enabled,
    }
    if permissions and permissions.get("network"):
        manifest["risk_level"] = "medium"
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{\"ok\": true}')\n", encoding="utf-8")
    (skill_dir / "tests" / "test_skill.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return skill_dir


def create_skill(db: Session, project_root: Path, name: str = "scheduled_skill", **kwargs: Any) -> Skill:
    file_kwargs = {key: value for key, value in kwargs.items() if key in {"enabled", "permissions", "schedule"}}
    skill_dir = write_installed_skill(project_root, name, **file_kwargs)
    skill = Skill(
        name=name,
        description="Scheduled test skill",
        status=kwargs.get("status", "installed"),
        risk_level="medium" if kwargs.get("permissions", {}).get("network") else "low",
        manifest_path=skill_dir.relative_to(project_root).as_posix() + "/manifest.json",
        installed_path=skill_dir.relative_to(project_root).as_posix(),
        enabled=kwargs.get("enabled", True),
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def service(db: Session, project_root: Path, fake_scheduler: FakeScheduler | None = None) -> SchedulerService:
    session_factory = sessionmaker(bind=db.bind, autoflush=False, autocommit=False)
    return SchedulerService(
        db,
        scheduler=fake_scheduler or FakeScheduler(),
        project_root=project_root,
        session_factory=session_factory,
    )


def daily_payload() -> ScheduleCreate:
    return ScheduleCreate(
        name="Morning run",
        schedule=SchedulePayload(type="daily", time="08:00", timezone="America/Toronto", input={"hello": "world"}),
    )


def approve_runtime(db: Session, skill: Skill, project_root: Path) -> None:
    permission_service = PermissionService(db, project_root=project_root)
    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)


def test_creates_daily_weekly_and_interval_schedules(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)

    daily, _ = scheduler.create_schedule(skill, daily_payload())
    weekly, _ = scheduler.create_schedule(
        skill,
        ScheduleCreate(
            name="Weekly run",
            schedule=SchedulePayload(type="weekly", day="monday", time="09:30", timezone="America/Toronto"),
        ),
    )
    interval, _ = scheduler.create_schedule(
        skill,
        ScheduleCreate(
            name="Interval run",
            schedule=SchedulePayload(type="interval", every=2, unit="hours", timezone="America/Toronto"),
        ),
    )

    assert daily.schedule_type == "daily"
    assert weekly.schedule_json["day"] == "monday"
    assert interval.schedule_json["every"] == 2
    assert daily.status == "pending"


def test_invalid_schedule_rejected(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)

    with pytest.raises(ValueError, match="daily schedules require time"):
        ScheduleCreate(name="Bad", schedule={"type": "daily", "timezone": "America/Toronto"})

    with pytest.raises(ScheduleError, match="supported IANA timezone"):
        service(db_session, tmp_path).create_schedule(
            skill,
            ScheduleCreate(
                name="Bad timezone",
                schedule=SchedulePayload(type="daily", time="08:00", timezone="Nope/Nope"),
            ),
        )


def test_proposed_and_disabled_skills_cannot_be_scheduled(tmp_path: Path, db_session: Session) -> None:
    proposed = create_skill(db_session, tmp_path, "proposed_skill", status="proposed")
    disabled = create_skill(db_session, tmp_path, "disabled_skill", enabled=False)
    scheduler = service(db_session, tmp_path)

    for skill, message in [
        (proposed, "Only installed skills can be scheduled"),
        (disabled, "Disabled skills cannot be scheduled"),
    ]:
        with pytest.raises(ScheduleError, match=message):
            scheduler.create_schedule(skill, daily_payload())


def test_schedule_requires_approval_before_activation(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    fake_scheduler = FakeScheduler()
    scheduler = service(db_session, tmp_path, fake_scheduler)

    schedule, approval = scheduler.create_schedule(skill, daily_payload())

    assert schedule.status == "pending"
    assert approval.status == "pending"
    assert fake_scheduler.jobs == {}


def test_approved_schedule_registers_job_and_denied_does_not(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    fake_scheduler = FakeScheduler()
    scheduler = service(db_session, tmp_path, fake_scheduler)
    schedule, _ = scheduler.create_schedule(skill, daily_payload())

    approved = scheduler.approve_schedule(schedule)

    assert approved.status == "active"
    assert fake_scheduler.jobs[scheduler.job_id(schedule.id)]["args"] == [schedule.id]
    assert approved.next_run_at == FakeJob.next_run_time.replace(tzinfo=None)

    denied_schedule, _ = scheduler.create_schedule(
        skill,
        ScheduleCreate(name="Deny me", schedule=SchedulePayload(type="daily", time="09:00")),
    )
    denied = scheduler.deny_schedule(denied_schedule)

    assert denied.status == "denied"
    assert scheduler.job_id(denied.id) not in fake_scheduler.jobs


def test_global_approval_endpoint_activates_schedule_on_shared_scheduler(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session, tmp_path)
    fake_scheduler = FakeScheduler()
    scheduler = service(db_session, tmp_path, fake_scheduler)
    schedule, approval = scheduler.create_schedule(skill, daily_payload())
    request_context = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(scheduler_service=scheduler))
    )

    updated = approve_permission_request(approval.id, request_context, db=db_session)

    assert updated.status == "approved"
    assert schedule.status == "active"
    assert scheduler.job_id(schedule.id) in fake_scheduler.jobs


def test_run_now_rejects_schedule_that_is_not_active(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule, _ = scheduler.create_schedule(skill, daily_payload())

    with pytest.raises(HTTPException, match="Only active approved schedules can run") as exc_info:
        run_schedule_now(schedule.id, db_session, scheduler)

    assert exc_info.value.status_code == 409


def test_pause_resume_and_delete_schedule_jobs(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    fake_scheduler = FakeScheduler()
    scheduler = service(db_session, tmp_path, fake_scheduler)
    schedule, _ = scheduler.create_schedule(skill, daily_payload())
    scheduler.approve_schedule(schedule)

    paused = scheduler.pause_schedule(schedule)
    assert paused.status == "paused"
    assert scheduler.job_id(schedule.id) not in fake_scheduler.jobs

    resumed = scheduler.resume_schedule(schedule)
    assert resumed.status == "active"
    assert scheduler.job_id(schedule.id) in fake_scheduler.jobs

    schedule_id = schedule.id
    scheduler.delete_schedule(schedule)
    assert db_session.get(SkillSchedule, schedule_id) is None
    assert scheduler.job_id(schedule_id) not in fake_scheduler.jobs


def test_scheduled_execution_uses_runtime_permission_checks(tmp_path: Path, db_session: Session) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule, _ = scheduler.create_schedule(skill, daily_payload())
    scheduler.approve_schedule(schedule)

    run = scheduler.run_scheduled_skill(schedule)

    assert run.status == "blocked"
    assert run.error_message == "Runtime permission request is pending"
    assert schedule.last_run_status == "blocked"


def test_network_requesting_skill_runs_after_runtime_approval(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_skill(
        db_session,
        tmp_path,
        permissions={
            "network": ["example.com"],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
    )
    approve_runtime(db_session, skill, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule, _ = scheduler.create_schedule(skill, daily_payload())
    scheduler.approve_schedule(schedule)

    from app.models import SkillRun
    from app.services import scheduler_service

    class FakeRunner:
        def __init__(self, db: Session) -> None:
            self.db = db

        def run(self, skill_id: int, skill_dir: Path, input_json: dict[str, Any]):
            run = SkillRun(
                skill_id=skill_id,
                status="succeeded",
                input_json=input_json,
                output_json={"network": "approved"},
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                exit_code=0,
            )
            self.db.add(run)
            self.db.commit()
            self.db.refresh(run)
            return run

    monkeypatch.setattr(scheduler_service, "get_skill_runner", lambda db: FakeRunner(db))

    run = scheduler.run_scheduled_skill(schedule)

    assert run.status == "succeeded"
    assert run.output_json == {"network": "approved"}


def test_scheduled_run_stores_result_with_schedule_marker(tmp_path: Path, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    skill = create_skill(db_session, tmp_path)
    approve_runtime(db_session, skill, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule, _ = scheduler.create_schedule(skill, daily_payload())
    scheduler.approve_schedule(schedule)

    from app.services import scheduler_service

    class FakeRunner:
        def __init__(self, db: Session) -> None:
            self.db = db

        def run(self, skill_id: int, skill_dir: Path, input_json: dict[str, Any]):
            from app.models import SkillRun

            run = SkillRun(
                skill_id=skill_id,
                status="succeeded",
                input_json=input_json,
                output_json={"ok": True},
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                exit_code=0,
            )
            self.db.add(run)
            self.db.commit()
            self.db.refresh(run)
            return run

    monkeypatch.setattr(scheduler_service, "get_skill_runner", lambda db: FakeRunner(db))

    run = scheduler.run_scheduled_skill(schedule)

    assert run.status == "succeeded"
    assert run.input_json["_schedule"]["schedule_id"] == schedule.id
    assert schedule.last_run_status == "succeeded"


def test_scheduler_failure_does_not_crash_app(tmp_path: Path, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    skill = create_skill(db_session, tmp_path)
    scheduler = service(db_session, tmp_path)
    schedule, _ = scheduler.create_schedule(skill, daily_payload())
    scheduler.approve_schedule(schedule)

    monkeypatch.setattr(SchedulerService, "run_scheduled_skill", lambda self, schedule: (_ for _ in ()).throw(RuntimeError("boom")))

    result = scheduler.execute_schedule(schedule.id)

    db_session.refresh(schedule)
    assert result is None
    assert schedule.last_run_status == "failed"
