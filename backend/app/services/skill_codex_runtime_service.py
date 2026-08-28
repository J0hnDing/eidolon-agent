from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Skill
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

    def call(
        self,
        skill: Skill,
        payload: SkillCodexRequest,
        *,
        expected_version_id: int | None = None,
    ) -> dict[str, object]:
        if skill.status != "installed":
            raise SkillCodexUnavailable("Only installed skills can call Codex")
        if not skill.enabled:
            raise SkillCodexUnavailable("Skill is disabled")
        if expected_version_id is not None and skill.active_version_id != expected_version_id:
            raise SkillCodexUnavailable("Runtime caller version is no longer active")

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

        if manifest.runtime not in {"function", "service"}:
            raise SkillCodexUnavailable(
                "web_app skills must use a scoped instance capability for privileged backend calls"
            )
        if manifest.runtime == "service" and expected_version_id is None:
            raise SkillCodexUnavailable(
                "Services can call Codex only during a schedule-attributed run"
            )
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

        return CodexService(
            self.db,
            project_root=self.project_root,
        ).skill_runtime_codex_call(
            skill,
            payload,
            internet_access=bool(internet_requested and internet_allowed),
        )
