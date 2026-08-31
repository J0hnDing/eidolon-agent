import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.models import (
    ScheduleOccurrence,
    ScheduleRuntimeState,
    Skill,
    SkillOperationLock,
    SkillRun,
    SkillSchedule,
)
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
        startup_at = utc_now()
        self.recover_interrupted_occurrences(startup_at)
        self.load_active_schedules()
        self.register_platform_services(now=startup_at)
        self.queue_startup_catchups(startup_at)
        if not getattr(self.scheduler, "running", False):
            self.scheduler.start()

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
                self._set_schedule_state(schedule, active_since_at=None)
                self.db.commit()

    def register_platform_services(self, *, now: datetime | None = None) -> None:
        if self.scheduler is None:
            return
        self._ensure_platform_state(now or utc_now())
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
            misfire_grace_time=None,
        )

    def execute_platform_service(self, service_id: str) -> dict[str, Any] | None:
        with self.session_factory() as db:
            service = SchedulerService(
                db,
                scheduler=self.scheduler,
                session_factory=self.session_factory,
                project_root=self.project_root,
            )
            now = utc_now()
            state = service._ensure_platform_state(now)
            scheduled_for_at = service._latest_platform_due_at(state, now)
            if scheduled_for_at is None:
                return None
            occurrence = service._claim_occurrence(
                schedule_key=service._platform_schedule_key(service_id),
                definition_fingerprint=state.definition_fingerprint,
                scheduled_for_at=scheduled_for_at,
                trigger_reason="automatic",
            )
            if occurrence is None:
                return None
            return service._execute_claimed_platform_occurrence(occurrence)

    def execute_claimed_platform_occurrence(self, occurrence_id: int) -> dict[str, Any] | None:
        with self.session_factory() as db:
            service = SchedulerService(
                db,
                scheduler=self.scheduler,
                session_factory=self.session_factory,
                project_root=self.project_root,
            )
            occurrence = db.get(ScheduleOccurrence, occurrence_id)
            if occurrence is None or occurrence.status != "claimed":
                return None
            return service._execute_claimed_platform_occurrence(occurrence)

    def _execute_claimed_platform_occurrence(
        self,
        occurrence: ScheduleOccurrence,
    ) -> dict[str, Any] | None:
        occurrence.status = "running"
        occurrence.started_at = utc_now()
        self.db.commit()
        try:
            result = PlatformServiceDispatcher(self.db).invoke(NOTION_DONE_CLEANUP_SERVICE_ID)
        except Exception as exc:
            self._finish_occurrence(occurrence, "failed", error_message=str(exc))
            self.platform_last_run_at = occurrence.ended_at
            self.platform_last_run_status = "failed"
            print(
                "Scheduled platform service "
                f"{NOTION_DONE_CLEANUP_SERVICE_ID} failed safely: {type(exc).__name__}"
            )
            return None
        status = str(result["status"])
        self._finish_occurrence(occurrence, status)
        self.platform_last_run_at = occurrence.ended_at
        self.platform_last_run_status = status
        print(
            f"Scheduled platform service {NOTION_DONE_CLEANUP_SERVICE_ID} completed with status "
            f"{status}; scanned={result['scanned_count']}; deleted={result['deleted_count']}"
        )
        return result

    def serialize_notion_done_cleanup_schedule(self) -> dict[str, Any]:
        job = None
        if self.scheduler is not None and hasattr(self.scheduler, "get_job"):
            try:
                job = self.scheduler.get_job(NOTION_DONE_CLEANUP_JOB_ID)
            except Exception:
                job = None
        latest_occurrence = self.db.scalar(
            select(ScheduleOccurrence)
            .where(
                ScheduleOccurrence.schedule_key
                == self._platform_schedule_key(NOTION_DONE_CLEANUP_SERVICE_ID)
            )
            .order_by(ScheduleOccurrence.scheduled_for_at.desc(), ScheduleOccurrence.id.desc())
            .limit(1)
        )
        state = self.db.get(
            ScheduleRuntimeState,
            self._platform_schedule_key(NOTION_DONE_CLEANUP_SERVICE_ID),
        )
        last_run_at = (
            latest_occurrence.ended_at or latest_occurrence.started_at
            if latest_occurrence is not None
            else None
        )
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
            "last_run_at": last_run_at,
            "last_run_status": latest_occurrence.status if latest_occurrence is not None else None,
            "created_at": state.created_at if state is not None else self.platform_started_at,
            "updated_at": last_run_at or (state.updated_at if state is not None else self.platform_started_at),
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
        self._set_schedule_state(schedule, active_since_at=None)
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
        previous_fingerprint = self._schedule_definition_fingerprint(schedule)
        schedule.name = payload.name
        schedule.schedule_type = schedule_data.type
        schedule.schedule_json = schedule_data.model_dump(exclude_none=True)
        schedule.input_json = schedule_data.input
        schedule.timezone = schedule_data.timezone
        definition_changed = previous_fingerprint != self._schedule_definition_fingerprint(schedule)
        try:
            if definition_changed:
                self._set_schedule_state(
                    schedule,
                    active_since_at=utc_now() if schedule.status == "active" else None,
                )
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
        self._set_schedule_state(schedule, active_since_at=None)
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def resume_schedule(self, schedule: SkillSchedule) -> SkillSchedule:
        self._validate_service(schedule.skill)
        if schedule.status != "paused":
            raise ScheduleError("Only paused service schedules can be resumed")
        self._validate_service_ready(schedule.skill, schedule.input_json)
        schedule.status = "active"
        self._set_schedule_state(schedule, active_since_at=utc_now())
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
                misfire_grace_time=None,
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
            state = self._ensure_schedule_state(schedule)
            if state.interval_anchor_at is not None:
                kwargs["start_date"] = self._as_utc(state.interval_anchor_at)
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
            now = utc_now()
            state = service._ensure_schedule_state(schedule, now=now)
            scheduled_for_at = service._latest_due_at(schedule, state, now)
            if scheduled_for_at is None:
                return None
            occurrence = service._claim_occurrence(
                schedule_key=service._skill_schedule_key(schedule.id),
                definition_fingerprint=state.definition_fingerprint,
                scheduled_for_at=scheduled_for_at,
                trigger_reason="automatic",
            )
            if occurrence is None:
                return None
            return service._execute_claimed_schedule_occurrence(schedule, occurrence)

    def execute_claimed_schedule_occurrence(self, occurrence_id: int) -> SkillRun | None:
        with self.session_factory() as db:
            service = SchedulerService(
                db,
                scheduler=self.scheduler,
                session_factory=self.session_factory,
                project_root=self.project_root,
            )
            occurrence = db.get(ScheduleOccurrence, occurrence_id)
            if occurrence is None or occurrence.status != "claimed":
                return None
            schedule_id = self._schedule_id_from_key(occurrence.schedule_key)
            schedule = db.get(SkillSchedule, schedule_id) if schedule_id is not None else None
            if schedule is None or schedule.status != "active":
                service._finish_occurrence(
                    occurrence,
                    "failed",
                    error_message="Schedule is no longer active",
                )
                return None
            return service._execute_claimed_schedule_occurrence(schedule, occurrence)

    def _execute_claimed_schedule_occurrence(
        self,
        schedule: SkillSchedule,
        occurrence: ScheduleOccurrence,
    ) -> SkillRun | None:
        occurrence.status = "running"
        occurrence.started_at = utc_now()
        self.db.commit()
        try:
            run = self.run_scheduled_service(
                schedule,
                schedule_occurrence_key=occurrence.occurrence_key,
                scheduled_for_at=self._as_utc(occurrence.scheduled_for_at),
                schedule_trigger=occurrence.trigger_reason,
            )
        except Exception as exc:
            schedule.last_run_at = utc_now()
            schedule.last_run_status = "failed"
            self._finish_occurrence(occurrence, "failed", error_message=str(exc))
            print(f"Scheduled service run failed for schedule {schedule.id}: {exc}")
            return None
        occurrence.skill_run_id = run.id
        self._finish_occurrence(occurrence, run.status, error_message=run.error_message)
        self.db.refresh(run)
        return run

    def run_scheduled_service(
        self,
        schedule: SkillSchedule,
        *,
        schedule_occurrence_key: str | None = None,
        scheduled_for_at: datetime | None = None,
        schedule_trigger: str | None = None,
    ) -> SkillRun:
        self._validate_service(schedule.skill)
        run = self._run_service_with_checks(
            schedule.skill,
            schedule.input_json,
            schedule.id,
            schedule_occurrence_key=schedule_occurrence_key,
            scheduled_for_at=scheduled_for_at,
            schedule_trigger=schedule_trigger,
        )
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
        *,
        schedule_occurrence_key: str | None = None,
        scheduled_for_at: datetime | None = None,
        schedule_trigger: str | None = None,
    ) -> SkillRun:
        if skill.status != "installed":
            return self._blocked_run(
                skill,
                input_json,
                schedule_id,
                "Only installed services can run",
                schedule_occurrence_key=schedule_occurrence_key,
                scheduled_for_at=scheduled_for_at,
                schedule_trigger=schedule_trigger,
            )
        if skill.runtime != "service":
            return self._blocked_run(
                skill,
                input_json,
                schedule_id,
                "Only services can run from schedules",
                schedule_occurrence_key=schedule_occurrence_key,
                scheduled_for_at=scheduled_for_at,
                schedule_trigger=schedule_trigger,
            )
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_run(skill)
        if not permission_decision.allowed:
            return self._blocked_run(
                skill,
                input_json,
                schedule_id,
                permission_decision.reason,
                schedule_occurrence_key=schedule_occurrence_key,
                scheduled_for_at=scheduled_for_at,
                schedule_trigger=schedule_trigger,
            )
        return ServiceRuntimeService(self.db, project_root=self.project_root).run(
            skill,
            input_json,
            schedule_id=schedule_id,
            schedule_occurrence_key=schedule_occurrence_key,
            scheduled_for_at=scheduled_for_at,
            schedule_trigger=schedule_trigger,
        )

    def _blocked_run(
        self,
        skill: Skill,
        input_json: dict[str, Any],
        schedule_id: int,
        reason: str,
        *,
        schedule_occurrence_key: str | None = None,
        scheduled_for_at: datetime | None = None,
        schedule_trigger: str | None = None,
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
            schedule_occurrence_key=schedule_occurrence_key,
            scheduled_for_at=scheduled_for_at,
            schedule_trigger=schedule_trigger,
            initiating_action=f"Scheduled service run {schedule_id}",
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def queue_startup_catchups(self, startup_at: datetime) -> None:
        if self.scheduler is None:
            return
        schedules = self.db.scalars(
            select(SkillSchedule)
            .join(Skill, Skill.id == SkillSchedule.skill_id)
            .where(SkillSchedule.status == "active")
            .where(Skill.runtime == "service")
        ).all()
        for schedule in schedules:
            state = self._ensure_schedule_state(schedule, now=startup_at)
            scheduled_for_at = self._latest_due_at(schedule, state, startup_at)
            if scheduled_for_at is None:
                continue
            occurrence = self._claim_occurrence(
                schedule_key=self._skill_schedule_key(schedule.id),
                definition_fingerprint=state.definition_fingerprint,
                scheduled_for_at=scheduled_for_at,
                trigger_reason="startup_catch_up",
            )
            if occurrence is not None:
                self._queue_claimed_occurrence(
                    self.execute_claimed_schedule_occurrence,
                    occurrence,
                )

        platform_state = self._ensure_platform_state(startup_at)
        platform_due_at = self._latest_platform_due_at(platform_state, startup_at)
        if platform_due_at is None:
            return
        platform_occurrence = self._claim_occurrence(
            schedule_key=self._platform_schedule_key(NOTION_DONE_CLEANUP_SERVICE_ID),
            definition_fingerprint=platform_state.definition_fingerprint,
            scheduled_for_at=platform_due_at,
            trigger_reason="startup_catch_up",
        )
        if platform_occurrence is not None:
            self._queue_claimed_occurrence(
                self.execute_claimed_platform_occurrence,
                platform_occurrence,
            )

    def _queue_claimed_occurrence(self, func: Any, occurrence: ScheduleOccurrence) -> None:
        try:
            self.scheduler.add_job(
                func,
                trigger="date",
                id=f"startup_schedule_occurrence_{occurrence.id}",
                args=[occurrence.id],
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=None,
            )
        except Exception as exc:
            self._finish_occurrence(
                occurrence,
                "failed",
                error_message=f"Startup catch-up could not be queued: {exc}",
            )

    def recover_interrupted_occurrences(self, recovered_at: datetime) -> None:
        interrupted = self.db.scalars(
            select(ScheduleOccurrence).where(ScheduleOccurrence.status.in_({"claimed", "running"}))
        ).all()
        for occurrence in interrupted:
            occurrence.status = "failed"
            occurrence.ended_at = recovered_at
            occurrence.error_message = (
                "Eidolon restarted after this occurrence was claimed; it will not be retried."
            )

        interrupted_runs = self.db.scalars(
            select(SkillRun)
            .where(SkillRun.invocation_source == "schedule")
            .where(SkillRun.status.in_({"pending", "running"}))
            .where(SkillRun.ended_at.is_(None))
        ).all()
        interrupted_skill_ids: set[int] = set()
        for run in interrupted_runs:
            run.status = "failed"
            run.ended_at = recovered_at
            run.error_message = (
                "Eidolon restarted while this scheduled run was active; it will not be retried."
            )
            interrupted_skill_ids.add(run.skill_id)
        if interrupted_skill_ids:
            self.db.execute(
                delete(SkillOperationLock)
                .where(SkillOperationLock.skill_id.in_(interrupted_skill_ids))
                .where(SkillOperationLock.operation == "run")
            )
        if interrupted or interrupted_runs:
            self.db.commit()

    def _claim_occurrence(
        self,
        *,
        schedule_key: str,
        definition_fingerprint: str,
        scheduled_for_at: datetime,
        trigger_reason: str,
    ) -> ScheduleOccurrence | None:
        scheduled_for_at = self._as_utc(scheduled_for_at)
        occurrence_key = self._occurrence_key(
            schedule_key,
            definition_fingerprint,
            scheduled_for_at,
        )
        occurrence = ScheduleOccurrence(
            occurrence_key=occurrence_key,
            schedule_key=schedule_key,
            definition_fingerprint=definition_fingerprint,
            scheduled_for_at=scheduled_for_at,
            trigger_reason=trigger_reason,
            status="claimed",
        )
        self.db.add(occurrence)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return None
        self.db.refresh(occurrence)
        return occurrence

    def _finish_occurrence(
        self,
        occurrence: ScheduleOccurrence,
        status: str,
        *,
        error_message: str | None = None,
    ) -> None:
        occurrence.status = status
        occurrence.ended_at = utc_now()
        occurrence.error_message = error_message
        self.db.commit()
        self.db.refresh(occurrence)

    def _ensure_schedule_state(
        self,
        schedule: SkillSchedule,
        *,
        now: datetime | None = None,
    ) -> ScheduleRuntimeState:
        now = self._as_utc(now or utc_now())
        schedule_key = self._skill_schedule_key(schedule.id)
        fingerprint = self._schedule_definition_fingerprint(schedule)
        state = self.db.get(ScheduleRuntimeState, schedule_key)
        if state is None:
            active_since_at = None
            if schedule.status == "active":
                active_since_at = self._as_utc(
                    schedule.last_run_at or schedule.updated_at or schedule.created_at
                )
            state = ScheduleRuntimeState(
                schedule_key=schedule_key,
                definition_fingerprint=fingerprint,
                active_since_at=active_since_at,
                interval_anchor_at=self._legacy_interval_anchor(schedule, active_since_at),
            )
            self.db.add(state)
            self.db.commit()
            self.db.refresh(state)
            return state
        if state.definition_fingerprint != fingerprint:
            self._set_schedule_state(
                schedule,
                active_since_at=now if schedule.status == "active" else None,
            )
            self.db.commit()
            self.db.refresh(state)
        elif schedule.status == "active" and state.active_since_at is None:
            self._set_schedule_state(schedule, active_since_at=now)
            self.db.commit()
            self.db.refresh(state)
        return state

    def _set_schedule_state(
        self,
        schedule: SkillSchedule,
        *,
        active_since_at: datetime | None,
    ) -> ScheduleRuntimeState:
        schedule_key = self._skill_schedule_key(schedule.id)
        state = self.db.get(ScheduleRuntimeState, schedule_key)
        if state is None:
            state = ScheduleRuntimeState(
                schedule_key=schedule_key,
                definition_fingerprint=self._schedule_definition_fingerprint(schedule),
            )
            self.db.add(state)
        state.definition_fingerprint = self._schedule_definition_fingerprint(schedule)
        state.active_since_at = self._as_utc(active_since_at) if active_since_at else None
        state.interval_anchor_at = (
            self._as_utc(active_since_at) + self._interval_delta(schedule)
            if active_since_at is not None and schedule.schedule_type == "interval"
            else None
        )
        return state

    def _ensure_platform_state(self, now: datetime) -> ScheduleRuntimeState:
        schedule_key = self._platform_schedule_key(NOTION_DONE_CLEANUP_SERVICE_ID)
        fingerprint = self._platform_definition_fingerprint()
        state = self.db.get(ScheduleRuntimeState, schedule_key)
        if state is None:
            state = ScheduleRuntimeState(
                schedule_key=schedule_key,
                definition_fingerprint=fingerprint,
                active_since_at=self._as_utc(now),
            )
            self.db.add(state)
            self.db.commit()
            self.db.refresh(state)
        elif state.definition_fingerprint != fingerprint:
            state.definition_fingerprint = fingerprint
            state.active_since_at = self._as_utc(now)
            state.interval_anchor_at = None
            self.db.commit()
            self.db.refresh(state)
        return state

    def _latest_due_at(
        self,
        schedule: SkillSchedule,
        state: ScheduleRuntimeState,
        now: datetime,
    ) -> datetime | None:
        active_since_at = (
            self._as_utc(state.active_since_at) if state.active_since_at is not None else None
        )
        now = self._as_utc(now)
        if active_since_at is None or active_since_at > now:
            return None
        if schedule.schedule_type == "interval":
            if state.interval_anchor_at is None:
                return None
            anchor = self._as_utc(state.interval_anchor_at)
            if anchor > now:
                return None
            interval = self._interval_delta(schedule)
            elapsed = now - anchor
            return anchor + interval * int(elapsed / interval)
        trigger = self.build_trigger(schedule)
        horizon = timedelta(days=2 if schedule.schedule_type == "daily" else 8)
        return self._latest_trigger_due_at(trigger, active_since_at, now, horizon)

    def _latest_platform_due_at(
        self,
        state: ScheduleRuntimeState,
        now: datetime,
    ) -> datetime | None:
        if state.active_since_at is None:
            return None
        active_since_at = self._as_utc(state.active_since_at)
        now = self._as_utc(now)
        if active_since_at > now:
            return None
        hour, minute = self._parse_time(NOTION_DONE_CLEANUP_TIME)
        if CronTrigger is None:
            trigger: Any = {
                "type": "daily",
                "hour": hour,
                "minute": minute,
                "timezone": NOTION_DONE_CLEANUP_TIMEZONE,
            }
        else:
            trigger = CronTrigger(
                hour=hour,
                minute=minute,
                timezone=NOTION_DONE_CLEANUP_TIMEZONE,
            )
        return self._latest_trigger_due_at(
            trigger,
            active_since_at,
            now,
            timedelta(days=2),
        )

    def _latest_trigger_due_at(
        self,
        trigger: Any,
        active_since_at: datetime,
        now: datetime,
        horizon: timedelta,
    ) -> datetime | None:
        search_from = max(active_since_at, now - horizon)
        if hasattr(trigger, "get_next_fire_time"):
            candidate = trigger.get_next_fire_time(None, search_from)
            latest: datetime | None = None
            while candidate is not None and self._as_utc(candidate) <= now:
                candidate_utc = self._as_utc(candidate)
                if candidate_utc >= active_since_at:
                    latest = candidate_utc
                candidate = trigger.get_next_fire_time(candidate, candidate)
            return latest
        return self._latest_mapping_trigger_due_at(trigger, active_since_at, now)

    def _latest_mapping_trigger_due_at(
        self,
        trigger: dict[str, Any],
        active_since_at: datetime,
        now: datetime,
    ) -> datetime | None:
        timezone = ZoneInfo(str(trigger["timezone"]))
        local_now = now.astimezone(timezone)
        days_back = 0
        if trigger["type"] == "weekly":
            days_back = (local_now.weekday() - int(trigger["day_of_week"])) % 7
        local_date = local_now.date() - timedelta(days=days_back)
        local_due = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            int(trigger["hour"]),
            int(trigger["minute"]),
            tzinfo=timezone,
        )
        due_at = local_due.astimezone(UTC)
        if due_at > now:
            due_at -= timedelta(days=7 if trigger["type"] == "weekly" else 1)
        return due_at if due_at >= active_since_at else None

    def _legacy_interval_anchor(
        self,
        schedule: SkillSchedule,
        active_since_at: datetime | None,
    ) -> datetime | None:
        if schedule.schedule_type != "interval" or active_since_at is None:
            return None
        if schedule.next_run_at is not None:
            return self._as_utc(schedule.next_run_at)
        return self._as_utc(active_since_at) + self._interval_delta(schedule)

    def _interval_delta(self, schedule: SkillSchedule) -> timedelta:
        data = schedule.schedule_json
        return timedelta(**{data["unit"]: data["every"]})

    def _schedule_definition_fingerprint(self, schedule: SkillSchedule) -> str:
        return self._fingerprint(
            {
                "schedule_type": schedule.schedule_type,
                "schedule": schedule.schedule_json,
                "input": schedule.input_json,
                "timezone": schedule.timezone,
            }
        )

    def _platform_definition_fingerprint(self) -> str:
        return self._fingerprint(
            {
                "service_id": NOTION_DONE_CLEANUP_SERVICE_ID,
                "type": "daily",
                "time": NOTION_DONE_CLEANUP_TIME,
                "timezone": NOTION_DONE_CLEANUP_TIMEZONE,
                "input": {},
            }
        )

    @staticmethod
    def _fingerprint(value: dict[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _occurrence_key(
        schedule_key: str,
        definition_fingerprint: str,
        scheduled_for_at: datetime,
    ) -> str:
        identity = (
            f"{schedule_key}:{definition_fingerprint}:"
            f"{scheduled_for_at.astimezone(UTC).isoformat(timespec='microseconds')}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _skill_schedule_key(schedule_id: int) -> str:
        return f"skill:{schedule_id}"

    @staticmethod
    def _platform_schedule_key(service_id: str) -> str:
        return f"platform:{service_id}"

    @staticmethod
    def _schedule_id_from_key(schedule_key: str) -> int | None:
        prefix, separator, raw_id = schedule_key.partition(":")
        if prefix != "skill" or not separator:
            return None
        try:
            return int(raw_id)
        except ValueError:
            return None

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

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
