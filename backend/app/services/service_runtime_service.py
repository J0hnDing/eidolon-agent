from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy.orm import Session

from app.models import Skill, SkillRun
from app.services.manifest_validator import validate_manifest_file
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import FunctionRunContext, get_skill_runner


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ServiceRuntimeService:
    db: Session
    project_root: Path | None = None
    runner_factory: Callable[[Session], Any] = get_skill_runner

    def __post_init__(self) -> None:
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)
        self.project_root = self.proposed_service.project_root

    def run(
        self,
        skill: Skill,
        input_json: dict[str, Any],
        *,
        schedule_id: int,
        schedule_occurrence_key: str | None = None,
        scheduled_for_at: datetime | None = None,
        schedule_trigger: str | None = None,
    ) -> SkillRun:
        manifest = validate_manifest_file(
            self.proposed_service.skill_dir_for_record(skill) / "manifest.json"
        )
        if manifest.runtime != "service":
            return self._blocked_run(
                skill,
                input_json,
                schedule_id,
                "Only service skills can run from schedules",
                schedule_occurrence_key=schedule_occurrence_key,
                scheduled_for_at=scheduled_for_at,
                schedule_trigger=schedule_trigger,
            )
        input_error = self._schema_error(input_json, manifest.input_schema, "input")
        if input_error:
            return self._blocked_run(
                skill,
                input_json,
                schedule_id,
                input_error,
                schedule_occurrence_key=schedule_occurrence_key,
                scheduled_for_at=scheduled_for_at,
                schedule_trigger=schedule_trigger,
            )

        capability_token = secrets.token_urlsafe(32)
        context = FunctionRunContext(
            version_id=skill.active_version_id,
            invocation_source="schedule",
            source_schedule_id=schedule_id,
            schedule_occurrence_key=schedule_occurrence_key,
            scheduled_for_at=scheduled_for_at,
            schedule_trigger=schedule_trigger,
            initiating_action=(
                f"Scheduled service {schedule_trigger} run {schedule_id}"
                if schedule_trigger
                else f"Scheduled service run {schedule_id}"
            ),
            capability_token=capability_token,
        )
        try:
            with SkillOperationGuard(self.db).locked(
                skill,
                "run",
                reason=f"Scheduled service run {schedule_id}",
            ):
                runner = self.runner_factory(self.db)
                try:
                    run = runner.run(
                        skill_id=skill.id,
                        skill_dir=self.proposed_service.skill_dir_for_record(skill),
                        input_json=input_json,
                        context=context,
                    )
                except TypeError as exc:
                    if "context" not in str(exc):
                        raise
                    run = runner.run(
                        skill_id=skill.id,
                        skill_dir=self.proposed_service.skill_dir_for_record(skill),
                        input_json=input_json,
                    )
        except SkillOperationConflict as exc:
            return self._blocked_run(
                skill,
                input_json,
                schedule_id,
                str(exc),
                schedule_occurrence_key=schedule_occurrence_key,
                scheduled_for_at=scheduled_for_at,
                schedule_trigger=schedule_trigger,
            )

        if run.output_json is not None:
            output_error = self._schema_error(run.output_json, manifest.output_schema, "output")
            if output_error:
                run.status = "failed"
                run.error_message = (
                    f"{run.error_message}; {output_error}" if run.error_message else output_error
                )
                run.ended_at = run.ended_at or utc_now()
                self.db.commit()
                self.db.refresh(run)
        return run

    def validate_input(self, skill: Skill, input_json: dict[str, Any]) -> None:
        manifest = validate_manifest_file(
            self.proposed_service.skill_dir_for_record(skill) / "manifest.json"
        )
        if manifest.runtime != "service":
            raise ValueError("Only service skills can have schedules")
        error = self._schema_error(input_json, manifest.input_schema, "input")
        if error:
            raise ValueError(error)

    @staticmethod
    def _schema_error(value: dict[str, Any], schema: dict[str, Any] | None, label: str) -> str | None:
        if schema is None:
            return f"Service {label} schema is missing"
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError as exc:
            path = ".".join(str(item) for item in exc.absolute_path)
            location = f" at {path}" if path else ""
            return f"Service {label} does not match its schema{location}: {exc.message}"
        return None

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
