from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator, SchemaError
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.skill_context import skill_execution_context
from app.models import Skill, WebAppInstance
from app.schemas.skill_codex import SkillCodexRequest
from app.services.codex_service import CodexService
from app.services.manifest_validator import validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService


class SkillCodexInvalidRequest(ValueError):
    pass


class SkillCodexUnavailable(ValueError):
    pass


@dataclass
class SkillCodexRuntimeService:
    db: Session
    project_root: Path

    def call_context(
        self,
        context: InvocationContext,
        payload: SkillCodexRequest,
    ) -> dict[str, object]:
        if payload.response_schema is not None:
            try:
                Draft202012Validator.check_schema(payload.response_schema)
            except SchemaError as exc:
                raise SkillCodexInvalidRequest("Codex response_schema is invalid") from exc
        if context.caller_skill_id is None:
            raise SkillCodexUnavailable("Codex calls require an authenticated skill caller")
        skill = self.db.get(Skill, context.caller_skill_id)
        if skill is None:
            raise SkillCodexUnavailable("Codex caller no longer exists")
        if skill.status != "installed":
            raise SkillCodexUnavailable("Only installed skills can call Codex")
        if not skill.enabled:
            raise SkillCodexUnavailable("Skill is disabled")
        if skill.active_version_id != context.caller_version_id:
            raise SkillCodexUnavailable("Runtime caller version is no longer active")

        if context.principal_kind == "skill":
            if skill.runtime not in {"function", "service"}:
                raise SkillCodexUnavailable("Runtime caller is not eligible to call Codex")
            if skill.runtime == "service" and context.source_schedule_id is None:
                raise SkillCodexUnavailable(
                    "Services can call Codex only during a schedule-attributed run"
                )
        elif context.principal_kind == "web_app":
            instance = self.db.get(WebAppInstance, context.web_app_instance_id)
            if (
                skill.runtime != "web_app"
                or instance is None
                or instance.status != "healthy"
                or instance.skill_id != skill.id
                or instance.version_id != context.caller_version_id
            ):
                raise SkillCodexUnavailable("Web application capability is no longer authorized")
        elif context.principal_kind == "user":
            if skill.runtime == "service":
                raise SkillCodexUnavailable(
                    "Services can call Codex only during a schedule-attributed run"
                )
            if skill.runtime != "function":
                raise SkillCodexUnavailable(
                    "Services and web applications require their scoped runtime capability"
                )
        else:
            raise SkillCodexUnavailable("Codex caller is not eligible")

        permission_decision = PermissionService(
            self.db,
            project_root=self.project_root,
        ).can_run(skill, include_integrations=False)
        if not permission_decision.allowed:
            raise SkillCodexUnavailable(permission_decision.reason)

        try:
            skill_dir = ProposedSkillService(
                self.db,
                project_root=self.project_root,
            ).skill_dir_for_record(skill)
            manifest = validate_manifest_file(skill_dir / "manifest.json")
        except (FileNotFoundError, ProposedSkillError, ValueError) as exc:
            raise SkillCodexInvalidRequest(str(exc)) from exc

        if manifest.runtime != skill.runtime:
            raise SkillCodexUnavailable("Active manifest runtime does not match the caller")
        if payload.codex_permissions.call_response is False:
            raise SkillCodexInvalidRequest("Codex call_response permission is required")
        if manifest.permissions.codex.call_response is False:
            raise SkillCodexUnavailable("Skill manifest does not allow Codex call/response")

        internet_requested = payload.codex_permissions.internet_access
        internet_allowed = bool(manifest.permissions.network)
        if internet_requested and not internet_allowed:
            raise SkillCodexUnavailable(
                "Codex internet access requires approved runtime network permission"
            )
        if internet_requested and not manifest.permissions.codex.internet_access:
            raise SkillCodexUnavailable("Skill manifest does not allow Codex internet access")

        with skill_execution_context(context.caller_skill_id):
            return CodexService(
                self.db,
                project_root=self.project_root,
            ).skill_runtime_codex_call(
                skill,
                payload,
                internet_access=bool(internet_requested and internet_allowed),
            )
