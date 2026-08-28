from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.models import Skill, SkillRun, SkillSchedule
from app.schemas.schedule import SchedulePayload, ScheduleUpdate
from app.services.permission_service import PermissionService
from app.services.platform_service import (
    NOTION_DONE_CLEANUP_SERVICE_ID,
    PlatformServiceDispatcher,
)
from app.services.proposed_skill_service import ProposedSkillService
from app.services.service_runtime_service import ServiceRuntimeService

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError:  # pragma: no cover - exercised through fake scheduler tests when dependency is absent.
    BackgroundScheduler = None  # type: ignore[assignment]
    CronTrigger = None  # type: ignore[assignment]
    IntervalTrigger = None  # type: ignore[assignment]


WEEKDAY_NUMBERS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
KNOWN_TIMEZONES = {
    "UTC",
    "America/Toronto",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
}
NOTION_DONE_CLEANUP_JOB_ID = "backend_notion_todo_cleanup_daily"
NOTION_DONE_CLEANUP_SCHEDULE_ID = 0
NOTION_DONE_CLEANUP_TIME = "03:00"
NOTION_DONE_CLEANUP_TIMEZONE = "America/Toronto"


class ScheduleError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SchedulerService:
    db: Session
    scheduler: Any | None = None
    session_factory: sessionmaker[Session] = SessionLocal
    project_root: Path | None = None
    platform_started_at: datetime = field(default_factory=utc_now, init=False)
    platform_last_run_at: datetime | None = field(default=None, init=False)
    platform_last_run_status: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.scheduler is None and BackgroundScheduler is not None:
            self.scheduler = BackgroundScheduler(timezone="UTC")
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)

    def start(self) -> None:
        if self.scheduler is None:
            return
        if not getattr(self.scheduler, "running", False):
            self.scheduler.start()
        self.load_active_schedules()
        self.register_platform_services()

    def shutdown(self) -> None:
        if self.scheduler is not None and getattr(self.scheduler, "running", False):
            self.scheduler.shutdown(wait=False)

    def load_active_schedules(self) -> None:
        schedules = self.db.scalars(
            select(SkillSchedule)
            .join(Skill, Skill.id == SkillSchedule.skill_id)
            .where(SkillSchedule.status == "active")
            .where(Skill.runtime == "service")
        ).all()
        for schedule in schedules:
            try:
                self.register_job(schedule)
            except Exception:
                schedule.status = "paused"
                schedule.next_run_at = None
                self.db.commit()

    def register_platform_services(self) -> None:
        if self.scheduler is None:
            return
        hour, minute = self._parse_time(NOTION_DONE_CLEANUP_TIME)
        trigger: Any
        if CronTrigger is None:
            trigger = {
                "type": "daily",
                "hour": hour,
                "minute": minute,
                "timezone": NOTION_DONE_CLEANUP_TIMEZONE,
            }
        else:
            trigger = CronTrigger(hour=hour, minute=minute, timezone=NOTION_DONE_CLEANUP_TIMEZONE)
        self.scheduler.add_job(
            self.execute_platform_service,
            trigger=trigger,
            id=NOTION_DONE_CLEANUP_JOB_ID,
            args=[NOTION_DONE_CLEANUP_SERVICE_ID],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    def execute_platform_service(self, service_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            try:
                result = PlatformServiceDispatcher(db).invoke(service_id)
            except Exception as exc:
                self.platform_last_run_at = utc_now()
                self.platform_last_run_status = "failed"
                print(f"Scheduled platform service {service_id} failed safely: {type(exc).__name__}")
                return None
            self.platform_last_run_at = utc_now()
            self.platform_last_run_status = str(result["status"])
            print(
                f"Scheduled platform service {service_id} completed with status {result['status']}; "
                f"scanned={result['scanned_count']}; deleted={result['deleted_count']}"
            )
            return result

    def serialize_notion_done_cleanup_schedule(self) -> dict[str, Any]:
        job = None
        if self.scheduler is not None and hasattr(self.scheduler, "get_job"):
            try:
                job = self.scheduler.get_job(NOTION_DONE_CLEANUP_JOB_ID)
            except Exception:
                job = None
        return {
            "id": NOTION_DONE_CLEANUP_SCHEDULE_ID,
            "schedule_kind": "platform",
            "service_id": NOTION_DONE_CLEANUP_SERVICE_ID,
            "read_only": True,
            "skill_id": None,
            "skill_name": None,
            "name": "Daily Notion Done Cleanup",
            "status": "active" if job is not None else "paused",
            "schedule_type": "daily",
            "schedule_json": {
                "type": "daily",
                "time": NOTION_DONE_CLEANUP_TIME,
                "timezone": NOTION_DONE_CLEANUP_TIMEZONE,
                "input": {},
            },
            "input_json": {},
            "timezone": NOTION_DONE_CLEANUP_TIMEZONE,
            "next_run_at": getattr(job, "next_run_time", None),
            "last_run_at": self.platform_last_run_at,
            "last_run_status": self.platform_last_run_status,
            "created_at": self.platform_started_at,
            "updated_at": self.platform_last_run_at or self.platform_started_at,
        }

    def create_from_manifest(self, skill: Skill) -> SkillSchedule:
        self._validate_service(skill)
        existing = self.db.scalar(select(SkillSchedule).where(SkillSchedule.skill_id == skill.id))
        if existing is not None:
            raise ScheduleError("Service already has its required schedule")
        from app.services.manifest_validator import validate_manifest_file

        manifest = validate_manifest_file(
            self.proposed_service.skill_dir_for_record(skill) / "manifest.json"
        )
        if manifest.runtime != "service" or manifest.schedule is None:
            raise ScheduleError("Service manifest must declare a schedule")
        payload = ScheduleUpdate(
            name=f"{skill.name} schedule",
            schedule=SchedulePayload(**manifest.schedule.model_dump()),
        )
        schedule_data = self._validated_schedule(payload.schedule)
        self._validate_input(skill, schedule_data.input)
        schedule = SkillSchedule(
            skill_id=skill.id,
            name=payload.name,
            status="paused",
            schedule_type=schedule_data.type,
            schedule_json=schedule_data.model_dump(exclude_none=True),
            input_json=schedule_data.input,
            timezone=schedule_data.timezone,
        )
        self.db.add(schedule)
        self.db.flush()
        return schedule

    def update_schedule(self, schedule: SkillSchedule, payload: ScheduleUpdate) -> SkillSchedule:
        self._validate_service(schedule.skill)
        schedule_data = self._validated_schedule(payload.schedule)
        if schedule.status == "active":
            self._validate_service_ready(schedule.skill, schedule_data.input)

        previous = {
            "name": schedule.name,
            "schedule_type": schedule.schedule_type,
            "schedule_json": dict(schedule.schedule_json),
            "input_json": dict(schedule.input_json),
            "timezone": schedule.timezone,
            "next_run_at": schedule.next_run_at,
        }
        schedule.name = payload.name
        schedule.schedule_type = schedule_data.type
        schedule.schedule_json = schedule_data.model_dump(exclude_none=True)
        schedule.input_json = schedule_data.input
        schedule.timezone = schedule_data.timezone
        try:
            if schedule.status == "active":
                self.register_job(schedule, commit=False)
            self.db.commit()
            self.db.refresh(schedule)
            return schedule
        except Exception as exc:
            self.db.rollback()
            for key, value in previous.items():
                setattr(schedule, key, value)
            if schedule.status == "active":
                try:
                    self.register_job(schedule)
                except Exception:
                    pass
            raise ScheduleError(f"Schedule could not be updated: {exc}") from exc

    def pause_schedule(self, schedule: SkillSchedule) -> SkillSchedule:
        self._validate_service(schedule.skill)
        if schedule.status != "active":
            raise ScheduleError("Only active service schedules can be paused")
        self.remove_job(schedule.id)
        schedule.status = "paused"
        schedule.next_run_at = None
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def resume_schedule(self, schedule: SkillSchedule) -> SkillSchedule:
        self._validate_service(schedule.skill)
        if schedule.status != "paused":
            raise ScheduleError("Only paused service schedules can be resumed")
        self._validate_service_ready(schedule.skill, schedule.input_json)
        schedule.status = "active"
        try:
            self.register_job(schedule)
        except Exception as exc:
            self.db.rollback()
            raise ScheduleError(f"Service schedule could not be activated: {exc}") from exc
        self.db.refresh(schedule)
        return schedule

    def register_job(self, schedule: SkillSchedule, *, commit: bool = True) -> None:
        self._validate_service(schedule.skill)
        trigger = self.build_trigger(schedule)
        if self.scheduler is None:
            schedule.next_run_at = None
        else:
            job = self.scheduler.add_job(
                self.execute_schedule,
                trigger=trigger,
                id=self.job_id(schedule.id),
                args=[schedule.id],
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            schedule.next_run_at = getattr(job, "next_run_time", None)
        if commit:
            self.db.commit()

    def remove_job(self, schedule_id: int) -> None:
        if self.scheduler is None:
            return
        try:
            self.scheduler.remove_job(self.job_id(schedule_id))
        except Exception:
            pass

    def build_trigger(self, schedule: SkillSchedule):
        data = schedule.schedule_json
        if schedule.schedule_type == "daily":
            hour, minute = self._parse_time(data["time"])
            if CronTrigger is None:
                return {"type": "daily", "hour": hour, "minute": minute, "timezone": schedule.timezone}
            return CronTrigger(hour=hour, minute=minute, timezone=schedule.timezone)
        if schedule.schedule_type == "weekly":
            hour, minute = self._parse_time(data["time"])
            weekday = WEEKDAY_NUMBERS[data["day"]]
            if CronTrigger is None:
                return {
                    "type": "weekly",
                    "day_of_week": weekday,
                    "hour": hour,
                    "minute": minute,
                    "timezone": schedule.timezone,
                }
            return CronTrigger(day_of_week=weekday, hour=hour, minute=minute, timezone=schedule.timezone)
        if schedule.schedule_type == "interval":
            kwargs = {data["unit"]: data["every"], "timezone": schedule.timezone}
            if IntervalTrigger is None:
                return {"type": "interval", **kwargs}
            return IntervalTrigger(**kwargs)
        raise ScheduleError("Unsupported schedule type")

    def execute_schedule(self, schedule_id: int) -> SkillRun | None:
        with self.session_factory() as db:
            service = SchedulerService(
                db,
                scheduler=self.scheduler,
                session_factory=self.session_factory,
                project_root=self.project_root,
            )
            schedule = db.get(SkillSchedule, schedule_id)
            if schedule is None or schedule.status != "active":
                return None
            try:
                return service.run_scheduled_service(schedule)
            except Exception as exc:
                schedule.last_run_at = utc_now()
                schedule.last_run_status = "failed"
                db.commit()
                print(f"Scheduled service run failed for schedule {schedule_id}: {exc}")
                return None

    def run_scheduled_service(self, schedule: SkillSchedule) -> SkillRun:
        self._validate_service(schedule.skill)
        run = self._run_service_with_checks(schedule.skill, schedule.input_json, schedule.id)
        schedule.last_run_at = run.ended_at or run.started_at or utc_now()
        schedule.last_run_status = run.status
        self.db.commit()
        self.db.refresh(schedule)
        return run

    def _run_service_with_checks(
        self,
        skill: Skill,
        input_json: dict[str, Any],
        schedule_id: int,
    ) -> SkillRun:
        if skill.status != "installed":
            return self._blocked_run(skill, input_json, schedule_id, "Only installed services can run")
        if skill.runtime != "service":
            return self._blocked_run(skill, input_json, schedule_id, "Only services can run from schedules")
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_run(skill)
        if not permission_decision.allowed:
            return self._blocked_run(skill, input_json, schedule_id, permission_decision.reason)
        return ServiceRuntimeService(self.db, project_root=self.project_root).run(
            skill,
            input_json,
            schedule_id=schedule_id,
        )

    def _blocked_run(
        self,
        skill: Skill,
        input_json: dict[str, Any],
        schedule_id: int,
        reason: str,
    ) -> SkillRun:
        run = SkillRun(
            skill_id=skill.id,
            version_id=skill.active_version_id,
            status="blocked",
            input_json=input_json,
            started_at=utc_now(),
            ended_at=utc_now(),
            error_message=reason,
            invocation_source="schedule",
            source_schedule_id=schedule_id,
            initiating_action=f"Scheduled service run {schedule_id}",
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def _validate_service(self, skill: Skill) -> None:
        if skill.status != "installed":
            raise ScheduleError("Only installed services can be scheduled")
        if skill.runtime != "service":
            raise ScheduleError("Only service skills can be scheduled")

    def _validate_service_ready(self, skill: Skill, input_json: dict[str, Any]) -> None:
        self._validate_service(skill)
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_run(skill)
        if not permission_decision.allowed:
            raise ScheduleError(permission_decision.reason)
        self._validate_input(skill, input_json)

    def _validate_input(self, skill: Skill, input_json: dict[str, Any]) -> None:
        try:
            ServiceRuntimeService(self.db, project_root=self.project_root).validate_input(skill, input_json)
        except (FileNotFoundError, ValueError) as exc:
            raise ScheduleError(str(exc)) from exc

    def _validated_schedule(self, payload: SchedulePayload) -> SchedulePayload:
        if payload.timezone not in KNOWN_TIMEZONES:
            raise ScheduleError("Schedule timezone must be a supported IANA timezone")
        return payload

    def human_schedule(self, schedule: SkillSchedule) -> str:
        data = schedule.schedule_json
        if schedule.schedule_type == "daily":
            return f"daily at {data['time']} {schedule.timezone}"
        if schedule.schedule_type == "weekly":
            return f"weekly on {data['day']} at {data['time']} {schedule.timezone}"
        if schedule.schedule_type == "interval":
            return f"every {data['every']} {data['unit']}"
        return schedule.schedule_type

    def _parse_time(self, value: str) -> tuple[int, int]:
        hour, minute = value.split(":")
        return int(hour), int(minute)

    def job_id(self, schedule_id: int) -> str:
        return f"service_schedule_{schedule_id}"


def serialize_schedule(schedule: SkillSchedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "schedule_kind": "service",
        "service_id": schedule.skill.name if schedule.skill else None,
        "read_only": False,
        "skill_id": schedule.skill_id,
        "skill_name": schedule.skill.name if schedule.skill else None,
        "name": schedule.name,
        "status": schedule.status,
        "schedule_type": schedule.schedule_type,
        "schedule_json": schedule.schedule_json,
        "input_json": schedule.input_json,
        "timezone": schedule.timezone,
        "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at,
        "last_run_status": schedule.last_run_status,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }
