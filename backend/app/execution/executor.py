from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.handlers import (
    AgentPrivateHandler,
    BackendCoreHandler,
    IntegrationHandler,
    UserFunctionHandler,
)
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.models import InvocationApproval
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
from app.services.integration_service import IntegrationService


class InvocationExecutor:
    """Single orchestration boundary for category-qualified catalog calls."""

    def __init__(
        self,
        db: Session,
        *,
        project_root=None,
        integration_service: IntegrationService | None = None,
    ) -> None:
        self.db = db
        self.project_root = project_root
        self.handlers = {
            "user": UserFunctionHandler(db, project_root=project_root),
            "integration": IntegrationHandler(
                db,
                project_root=project_root,
                service=integration_service,
            ),
            "backend_core": BackendCoreHandler(db, project_root=project_root),
            "agent_private": AgentPrivateHandler(db),
        }

    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        self._reauthorize_agent(context, target.target_id)
        handler = self.handlers.get(target.category)
        if handler is None:
            raise InvocationExecutionError("invalid_target", "Invocation category is unsupported")
        return handler.execute(target, input_json, context)

    def execute_approved(self, approval: InvocationApproval) -> InvocationOutcome:
        if not isinstance(approval, InvocationApproval):
            raise InvocationExecutionError("invalid_approval", "A claimed invocation approval row is required")
        if approval.decision_status != "approved" or approval.execution_status != "executing":
            raise InvocationExecutionError("invalid_approval", "Invocation approval is not claimed for execution")
        context = InvocationContextFactory(
            self.db,
            project_root=self.project_root,
        ).from_approval(approval)
        self._reauthorize_agent(context, approval.target_id)
        category = {
            "user_function": "user",
            "integration": "integration",
        }.get(approval.target_kind)
        if category is None:
            raise InvocationExecutionError("invalid_target", "Invocation approval target is unsupported")
        return self.handlers[category].execute_approved(approval, context)

    def _reauthorize_agent(self, context: InvocationContext, target_id: str) -> None:
        if context.principal_kind != "agent":
            return
        if context.agent_id is None or context.agent_session_id is None:
            raise InvocationExecutionError("agent_permission_denied", "Agent authority is incomplete")
        try:
            policy = AgentPolicyService(self.db)
            policy.require_session(context.agent_id, context.agent_session_id)
            policy.require_function(context.agent_id, target_id)
        except AgentPermissionError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
