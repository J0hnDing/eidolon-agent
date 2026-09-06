from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.models import InvocationApproval, Skill
from app.services.function_registry_service import FunctionRegistryError, FunctionRegistryService


class UserFunctionHandler:
    def __init__(self, db: Session, *, project_root=None, registry: FunctionRegistryService | None = None) -> None:
        self.db = db
        self.project_root = project_root
        self.registry = registry or FunctionRegistryService(db, project_root=project_root)

    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        skill = self.db.scalar(select(Skill).where(Skill.name == target.target_id))
        if skill is None or skill.runtime != "function":
            raise InvocationExecutionError("function_unavailable", "Target function is not installed")
        caller_error = self.registry.caller_authorization_error(skill, context)
        if caller_error is not None:
            run = self.registry.blocked_run_for_context(skill, input_json, caller_error, context)
            return self._run_outcome(run)
        result = self.registry.execute_resolved(skill, input_json, context)
        if isinstance(result, InvocationApproval):
            return InvocationOutcome(
                status="pending_approval",
                output={"status": "pending_approval", "approval_id": result.id},
                approval_id=result.id,
            )
        return self._run_outcome(result)

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if approval.target_kind != "user_function":
            raise InvocationExecutionError("invalid_target", "Approval is not for a user function")
        if context.principal_kind == "web_app":
            try:
                InvocationContextFactory(self.db, project_root=self.project_root).require_current_web_app(context)
            except FunctionRegistryError as exc:
                raise InvocationExecutionError("stale_contract", str(exc)) from None
        run = self.registry.execute_claimed_approval(approval, context)
        outcome = self._run_outcome(run)
        if outcome.status not in {"succeeded", "partial"}:
            raise InvocationExecutionError(
                "function_failed",
                outcome.error_message or f"Approved function invocation {outcome.status}",
            )
        return outcome

    @staticmethod
    def _run_outcome(run) -> InvocationOutcome:
        error_type = None
        if run.status not in {"succeeded", "partial"}:
            error_type = "function_blocked" if run.status == "blocked" else "function_failed"
        return InvocationOutcome(
            status=run.status,
            output=run.output_json if isinstance(run.output_json, dict) else None,
            skill_run_id=run.id,
            error_type=error_type,
            error_message=run.error_message,
        )
