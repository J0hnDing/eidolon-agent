from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    IntegrationAuditRecord,
    McpAuditRecord,
    ScheduleOccurrence,
    Skill,
    SkillRun,
    WebAppInstance,
)


@dataclass
class RuntimeStateService:
    db: Session

    def running_skill_ids(self) -> set[int]:
        run_skill_ids = self.db.scalars(
            select(SkillRun.skill_id)
            .where(SkillRun.status == "running")
            .where(SkillRun.ended_at.is_(None))
        ).all()
        web_app_skill_ids = self.db.scalars(
            select(WebAppInstance.skill_id)
            .where(WebAppInstance.status.in_({"starting", "ready", "healthy"}))
            .where(WebAppInstance.stopped_at.is_(None))
        ).all()
        return {*run_skill_ids, *web_app_skill_ids}

    def running_function_ids(self) -> set[str]:
        user_function_ids = self.db.scalars(
            select(Skill.name)
            .join(SkillRun, SkillRun.skill_id == Skill.id)
            .where(Skill.runtime == "function")
            .where(SkillRun.status == "running")
            .where(SkillRun.ended_at.is_(None))
        ).all()
        integration_function_ids = self.db.scalars(
            select(IntegrationAuditRecord.operation_id)
            .where(IntegrationAuditRecord.status == "running")
            .where(IntegrationAuditRecord.completed_at.is_(None))
        ).all()
        mcp_function_ids = self.db.scalars(
            select(McpAuditRecord.function_id)
            .where(McpAuditRecord.status == "running")
            .where(McpAuditRecord.completed_at.is_(None))
        ).all()
        return {*user_function_ids, *integration_function_ids, *mcp_function_ids}

    def running_schedule_ids(self) -> set[int]:
        schedule_ids = set(
            self.db.scalars(
                select(SkillRun.source_schedule_id)
                .where(SkillRun.source_schedule_id.is_not(None))
                .where(SkillRun.status == "running")
                .where(SkillRun.ended_at.is_(None))
            ).all()
        )
        occurrence_keys = self.db.scalars(
            select(ScheduleOccurrence.schedule_key)
            .where(ScheduleOccurrence.status == "running")
            .where(ScheduleOccurrence.ended_at.is_(None))
            .where(ScheduleOccurrence.schedule_key.like("skill:%"))
        ).all()
        for schedule_key in occurrence_keys:
            _, _, raw_id = schedule_key.partition(":")
            try:
                schedule_ids.add(int(raw_id))
            except ValueError:
                continue
        return {schedule_id for schedule_id in schedule_ids if schedule_id is not None}

    def platform_service_is_running(self, service_id: str) -> bool:
        return (
            self.db.scalar(
                select(ScheduleOccurrence.id)
                .where(ScheduleOccurrence.schedule_key == f"platform:{service_id}")
                .where(ScheduleOccurrence.status == "running")
                .where(ScheduleOccurrence.ended_at.is_(None))
                .limit(1)
            )
            is not None
        )
