from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.models import ActTurn, InvocationApproval, Skill, SkillRun, WebAppInstance
from app.services.agent_policy_service import AgentPolicyService
from app.services.function_registry_service import FunctionRegistryError
from app.services.permission_service import PermissionService
from app.services.web_app_runtime_service import WebAppRuntimeService


class InvocationContextFactory:
    def __init__(self, db: Session, *, project_root=None, web_app_runtime=None) -> None:
        self.db = db
        self.project_root = project_root
        self.web_app_runtime = web_app_runtime

    def from_agent_credential(self, credential: str) -> InvocationContext:
        agent_id, session_id = AgentPolicyService(self.db).authenticate(credential)
        turn_id = self.db.scalar(
            select(ActTurn.id)
            .where(ActTurn.session_id == session_id, ActTurn.status == "running")
            .order_by(ActTurn.id.desc())
        )
        return InvocationContext(
            principal_kind="agent",
            origin="agent_mcp",
            agent_id=agent_id,
            agent_session_id=session_id,
            agent_turn_id=turn_id,
            initiating_action=f"{agent_id}_mcp",
        )

    def from_runtime_capability(
        self,
        token: str,
        *,
        initiating_action: str | None = None,
    ) -> InvocationContext:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        run = self.db.scalar(
            select(SkillRun)
            .where(SkillRun.function_capability_token_hash == token_hash)
            .where(SkillRun.status == "running")
            .where(SkillRun.ended_at.is_(None))
            .order_by(SkillRun.id.desc())
        )
        if run is None:
            raise FunctionRegistryError("Function caller capability is invalid or expired")
        caller = self.db.get(Skill, run.skill_id)
        if caller is None or run.version_id is None:
            raise FunctionRegistryError("Function caller identity is no longer available")
        valid_runtime = caller.runtime == "function" or (
            caller.runtime == "service"
            and run.invocation_source == "schedule"
            and run.source_schedule_id is not None
        )
        if (
            caller.status != "installed"
            or not valid_runtime
            or not caller.enabled
            or caller.active_version_id != run.version_id
        ):
            raise FunctionRegistryError(
                "Runtime caller is no longer installed, eligible, and version-current"
            )
        decision = PermissionService(self.db, project_root=self.project_root).can_run(caller)
        if not decision.allowed:
            raise FunctionRegistryError(decision.reason)
        return InvocationContext(
            principal_kind="skill",
            origin="skill_runtime",
            caller_skill_id=caller.id,
            caller_version_id=run.version_id,
            caller_run_id=run.id,
            caller_runtime=caller.runtime,
            source_schedule_id=run.source_schedule_id,
            initiating_action=initiating_action or f"function_call_from_run_{run.id}",
        )

    def from_web_app_capability(
        self,
        token: str,
        *,
        initiating_action: str | None = None,
    ) -> InvocationContext:
        runtime = self.web_app_runtime or WebAppRuntimeService(
            self.db,
            project_root=self.project_root,
        )
        instance, skill, _manifest = runtime.instance_for_capability(token)
        return InvocationContext(
            principal_kind="web_app",
            origin="web_app_runtime",
            caller_skill_id=skill.id,
            caller_version_id=instance.version_id,
            caller_runtime="web_app",
            web_app_instance_id=instance.id,
            initiating_action=initiating_action or f"web_app_instance_{instance.id}",
        )

    @staticmethod
    def direct_user(
        *,
        origin: str = "http",
        initiating_action: str | None = None,
        caller_skill_id: int | None = None,
        caller_version_id: int | None = None,
        caller_runtime: str | None = None,
    ) -> InvocationContext:
        return InvocationContext(
            principal_kind="user",
            origin=origin,
            caller_skill_id=caller_skill_id,
            caller_version_id=caller_version_id,
            caller_runtime=caller_runtime,
            initiating_action=initiating_action,
        )

    @staticmethod
    def trusted_system(
        system_principal: str,
        *,
        origin: str = "backend",
        source_schedule_id: int | None = None,
        initiating_action: str | None = None,
    ) -> InvocationContext:
        return InvocationContext(
            principal_kind="system",
            origin=origin,
            system_principal=system_principal,
            source_schedule_id=source_schedule_id,
            initiating_action=initiating_action,
        )

    def from_approval(self, approval: InvocationApproval) -> InvocationContext:
        metadata = approval.dispatch_metadata_json or {}
        serialized = metadata.get("invocation_context_v1")
        if isinstance(serialized, dict):
            return InvocationContext.deserialize(serialized)

        legacy_agent = metadata.get("agent_identity")
        if isinstance(legacy_agent, dict):
            return InvocationContext(
                principal_kind="agent",
                origin="agent_mcp",
                agent_id=str(legacy_agent.get("agent_id")),
                agent_session_id=int(legacy_agent.get("session_id")),
                initiating_action=approval.initiating_action,
            )
        if approval.web_app_instance_id is not None:
            return InvocationContext(
                principal_kind="web_app",
                origin="web_app_runtime",
                caller_skill_id=approval.caller_skill_id,
                caller_version_id=approval.caller_version_id,
                caller_runtime="web_app",
                web_app_instance_id=approval.web_app_instance_id,
                initiating_action=approval.initiating_action,
            )
        if approval.caller_skill_id is not None:
            skill = self.db.get(Skill, approval.caller_skill_id)
            caller_run = self.db.get(SkillRun, approval.caller_run_id)
            return InvocationContext(
                principal_kind="skill",
                origin="skill_runtime",
                caller_skill_id=approval.caller_skill_id,
                caller_version_id=approval.caller_version_id,
                caller_run_id=approval.caller_run_id,
                caller_runtime=skill.runtime if skill is not None else None,
                source_schedule_id=(
                    caller_run.source_schedule_id if caller_run is not None else None
                ),
                initiating_action=approval.initiating_action,
            )
        if approval.caller_type in {"system", "backend"} or approval.source == "backend":
            return self.trusted_system(
                "legacy_backend",
                initiating_action=approval.initiating_action,
            )
        return self.direct_user(
            origin="codex_mcp" if approval.source in {"mcp", "codex_mcp"} else "http",
            initiating_action=approval.initiating_action,
        )

    def require_current_web_app(self, context: InvocationContext) -> tuple[WebAppInstance, Skill]:
        instance = self.db.get(WebAppInstance, context.web_app_instance_id)
        skill = self.db.get(Skill, context.caller_skill_id)
        if (
            instance is None
            or skill is None
            or instance.status != "healthy"
            or instance.skill_id != skill.id
            or instance.version_id != context.caller_version_id
            or skill.status != "installed"
            or not skill.enabled
            or skill.runtime != "web_app"
            or skill.active_version_id != instance.version_id
        ):
            raise FunctionRegistryError("Web application caller is no longer authorized")
        return instance, skill
