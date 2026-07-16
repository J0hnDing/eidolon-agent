from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.models import ApprovalRequest, Skill, SkillRun, SkillSchedule
from app.schemas.schedule import ScheduleCreate, SchedulePayload
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import get_skill_runner

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

    def shutdown(self) -> None:
        if self.scheduler is not None and getattr(self.scheduler, "running", False):
            self.scheduler.shutdown(wait=False)

    def load_active_schedules(self) -> None:
        for schedule in self.db.scalars(select(SkillSchedule).where(SkillSchedule.status == "active")).all():
            self.register_job(schedule)

    def create_schedule(
        self,
        skill: Skill,
        payload: ScheduleCreate,
        *,
        allow_disabled: bool = False,
    ) -> tuple[SkillSchedule, ApprovalRequest]:
        self._validate_skill_can_be_scheduled(skill, allow_disabled=allow_disabled)
        schedule_data = self._validated_schedule(payload.schedule)
        schedule = SkillSchedule(
            skill_id=skill.id,
            name=payload.name,
            status="pending",
            schedule_type=schedule_data.type,
            schedule_json=schedule_data.model_dump(exclude_none=True),
            input_json=schedule_data.input,
            timezone=schedule_data.timezone,
        )
        self.db.add(schedule)
        self.db.commit()
        self.db.refresh(schedule)

        approval = self._create_schedule_approval(skill, schedule)
        return schedule, approval

    def create_from_manifest_if_present(self, skill: Skill) -> tuple[SkillSchedule, ApprovalRequest] | None:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        from app.services.manifest_validator import validate_manifest_file

        manifest = validate_manifest_file(skill_dir / "manifest.json")
        if manifest.schedule is None:
            return None
        self._validate_skill_can_be_scheduled(skill, allow_disabled=True)
        payload = ScheduleCreate(
            name=f"{skill.name} declared schedule",
            schedule=SchedulePayload(**manifest.schedule.model_dump()),
        )
        return self.create_schedule(skill, payload, allow_disabled=True)

    def approve_schedule(self, schedule: SkillSchedule) -> SkillSchedule:
        approval = self._latest_schedule_approval(schedule)
        if approval is None:
            approval = self._create_schedule_approval(schedule.skill, schedule)
        if approval.risk_level == "blocked":
            raise ScheduleError("Blocked schedule approvals cannot be activated")
        approval.status = "approved"
        approval.resolved_at = utc_now()
        approval.resolved_by = "local_user"
        schedule.status = "active"
        self.db.commit()
        self.db.refresh(schedule)
        self.register_job(schedule)
        return schedule

    def deny_schedule(self, schedule: SkillSchedule) -> SkillSchedule:
        approval = self._latest_schedule_approval(schedule)
        if approval is None:
            approval = self._create_schedule_approval(schedule.skill, schedule)
        if approval.status != "approved":
            approval.status = "denied"
            approval.resolved_at = utc_now()
            approval.resolved_by = "local_user"
        schedule.status = "denied"
        self.remove_job(schedule.id)
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def pause_schedule(self, schedule: SkillSchedule) -> SkillSchedule:
        if schedule.status != "active":
            raise ScheduleError("Only active schedules can be paused")
        schedule.status = "paused"
        self.remove_job(schedule.id)
        self.db.commit()
        self.db.refresh(schedule)
        return schedule

    def resume_schedule(self, schedule: SkillSchedule) -> SkillSchedule:
        approval = self._latest_schedule_approval(schedule)
        if approval is None or approval.status != "approved":
            raise ScheduleError("Schedule approval is required before resume")
        schedule.status = "active"
        self.db.commit()
        self.db.refresh(schedule)
        self.register_job(schedule)
        return schedule

    def delete_schedule(self, schedule: SkillSchedule) -> None:
        self.remove_job(schedule.id)
        self.db.query(ApprovalRequest).filter(ApprovalRequest.schedule_id == schedule.id).delete(
            synchronize_session=False
        )
        self.db.delete(schedule)
        self.db.commit()

    def register_job(self, schedule: SkillSchedule) -> None:
        if self.scheduler is None:
            schedule.next_run_at = None
            self.db.commit()
            return
        trigger = self.build_trigger(schedule)
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
            service = SchedulerService(db, scheduler=self.scheduler, session_factory=self.session_factory)
            schedule = db.get(SkillSchedule, schedule_id)
            if schedule is None or schedule.status != "active":
                return None
            try:
                run = service.run_scheduled_skill(schedule)
            except Exception as exc:
                schedule.last_run_at = utc_now()
                schedule.last_run_status = "failed"
                db.commit()
                print(f"Scheduled skill run failed for schedule {schedule_id}: {exc}")
                return None
            return run

    def run_scheduled_skill(self, schedule: SkillSchedule) -> SkillRun:
        skill = schedule.skill
        run = self._run_skill_with_checks(skill, schedule.input_json, source_schedule_id=schedule.id)
        schedule.last_run_at = run.ended_at or run.started_at or utc_now()
        schedule.last_run_status = run.status
        self.db.commit()
        self.db.refresh(schedule)
        return run

    def _run_skill_with_checks(
        self,
        skill: Skill,
        input_json: dict[str, Any],
        *,
        source_schedule_id: int,
    ) -> SkillRun:
        if skill.status != "installed":
            return self._blocked_run(skill.id, input_json, "Only installed skills can be run by a schedule")
        if skill.runtime != "function":
            return self._blocked_run(skill.id, input_json, "Persistent web_app skills cannot use bounded schedules")
        if not skill.enabled:
            return self._blocked_run(skill.id, input_json, "Skill is disabled")
        approval = self._latest_schedule_approval_by_id(source_schedule_id)
        if approval is None or approval.status != "approved":
            return self._blocked_run(skill.id, input_json, "Schedule approval is required before scheduled execution")
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_run(skill)
        if not permission_decision.allowed:
            return self._blocked_run(skill.id, input_json, permission_decision.reason)
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        scheduled_input = {"_schedule": {"schedule_id": source_schedule_id}, **input_json}
        try:
            with SkillOperationGuard(self.db).locked(skill, "run", reason=f"Scheduled run {source_schedule_id}"):
                return get_skill_runner(self.db).run(skill_id=skill.id, skill_dir=skill_dir, input_json=scheduled_input)
        except SkillOperationConflict as exc:
            return self._blocked_run(skill.id, scheduled_input, str(exc))

    def _blocked_run(self, skill_id: int, input_json: dict[str, Any], reason: str) -> SkillRun:
        run = SkillRun(
            skill_id=skill_id,
            status="blocked",
            input_json=input_json,
            started_at=utc_now(),
            ended_at=utc_now(),
            error_message=reason,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def _validate_skill_can_be_scheduled(self, skill: Skill, *, allow_disabled: bool = False) -> None:
        if skill.status != "installed":
            raise ScheduleError("Only installed skills can be scheduled")
        if skill.runtime != "function":
            raise ScheduleError("Persistent web_app skills cannot be scheduled as bounded runs")
        if not skill.enabled and not allow_disabled:
            raise ScheduleError("Disabled skills cannot be scheduled")

    def _validated_schedule(self, payload: SchedulePayload) -> SchedulePayload:
        if payload.timezone not in KNOWN_TIMEZONES:
            raise ScheduleError("Schedule timezone must be a supported IANA timezone")
        return payload

    def _create_schedule_approval(self, skill: Skill, schedule: SkillSchedule) -> ApprovalRequest:
        existing = self._latest_schedule_approval(schedule)
        if existing and existing.status in {"pending", "approved", "denied"}:
            return existing
        runtime_request = PermissionService(self.db, project_root=self.project_root).create_runtime_request(skill)
        explanation = (
            f"Approve schedule '{schedule.name}' for skill {skill.name}. "
            f"It will run {self.human_schedule(schedule)} with input JSON {schedule.input_json}. "
            "Approving this schedule does not bypass runtime permission checks; every scheduled run must still pass "
            "installed/enabled/runtime-permission/runner support checks."
        )
        request = ApprovalRequest(
            skill_id=skill.id,
            schedule_id=schedule.id,
            request_scope="runtime",
            request_type="schedule",
            risk_level=runtime_request.risk_level,
            requested_permissions_json=runtime_request.requested_permissions_json,
            requested_dependencies_json=runtime_request.requested_dependencies_json,
            requested_network_domains_json=runtime_request.requested_network_domains_json,
            requested_filesystem_json=runtime_request.requested_filesystem_json,
            reason_json={"schedule": schedule.schedule_json, "input": schedule.input_json},
            reason=explanation,
            user_explanation=explanation,
            status="pending",
        )
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)
        return request

    def _latest_schedule_approval(self, schedule: SkillSchedule) -> ApprovalRequest | None:
        return self._latest_schedule_approval_by_id(schedule.id)

    def _latest_schedule_approval_by_id(self, schedule_id: int) -> ApprovalRequest | None:
        return self.db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.schedule_id == schedule_id)
            .where(ApprovalRequest.request_type == "schedule")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        )

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
        return f"skill_schedule_{schedule_id}"


def serialize_schedule(schedule: SkillSchedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
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
