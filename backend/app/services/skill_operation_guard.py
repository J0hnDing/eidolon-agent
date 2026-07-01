from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Skill, SkillOperationLock, SkillRun


RUN_OPERATIONS = {"run"}
MUTATING_OPERATIONS = {"install", "update", "repair", "delete"}
ACTIVE_RUN_STATUSES = {"pending", "running"}
DEFAULT_STALE_AFTER = timedelta(hours=2)
OPERATION_LABELS = {
    "install": "installed",
    "update": "updated",
    "repair": "repaired",
    "delete": "deleted",
}


class SkillOperationConflict(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SkillOperationGuard:
    db: Session
    stale_after: timedelta = DEFAULT_STALE_AFTER

    @contextmanager
    def locked(self, skill: Skill, operation: str, reason: str | None = None) -> Iterator[None]:
        self.acquire(skill, operation, reason=reason)
        try:
            yield
        finally:
            self.release(skill.id)

    def acquire(self, skill: Skill, operation: str, reason: str | None = None) -> SkillOperationLock:
        if operation in RUN_OPERATIONS and self.has_active_run(skill.id):
            raise SkillOperationConflict(f"Skill {skill.name} already has an active run")
        if operation in MUTATING_OPERATIONS and self.has_active_run(skill.id):
            label = OPERATION_LABELS.get(operation, f"{operation}ed")
            raise SkillOperationConflict(f"Skill {skill.name} has an active run and cannot be {label}")

        self._clear_stale_lock(skill.id)
        existing = self.db.get(SkillOperationLock, skill.id)
        if existing is not None:
            raise SkillOperationConflict(
                f"Skill {skill.name} is busy with {existing.operation}; try again after it finishes"
            )
        lock = SkillOperationLock(
            skill_id=skill.id,
            operation=operation,
            reason=reason,
        )
        self.db.add(lock)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            existing = self.db.get(SkillOperationLock, skill.id)
            if existing is None:
                raise SkillOperationConflict(f"Skill {skill.name} is already busy") from exc
            raise SkillOperationConflict(
                f"Skill {skill.name} is busy with {existing.operation}; try again after it finishes"
            ) from exc
        self.db.refresh(lock)
        return lock

    def release(self, skill_id: int) -> None:
        lock = self.db.get(SkillOperationLock, skill_id)
        if lock is None:
            return
        self.db.delete(lock)
        self.db.commit()

    def has_active_run(self, skill_id: int) -> bool:
        return (
            self.db.scalar(
                select(SkillRun.id)
                .where(SkillRun.skill_id == skill_id)
                .where(SkillRun.status.in_(ACTIVE_RUN_STATUSES))
                .where(SkillRun.ended_at.is_(None))
                .limit(1)
            )
            is not None
        )

    def _clear_stale_lock(self, skill_id: int) -> None:
        lock = self.db.get(SkillOperationLock, skill_id)
        if lock is None:
            return
        created_at = lock.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if utc_now() - created_at <= self.stale_after:
            return
        if self.has_active_run(skill_id):
            return
        self.db.delete(lock)
        self.db.commit()
