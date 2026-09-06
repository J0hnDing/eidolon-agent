from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.models import InvocationApproval
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
from app.services.agent_proposal_service import AgentProposalService


class AgentPrivateHandler:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if target.target_id != "plan_approval_request":
            raise InvocationExecutionError("not_exposed", "Agent-private target is unavailable")
        if (
            context.principal_kind != "agent"
            or context.agent_id != "assistant"
            or context.agent_session_id is None
        ):
            raise InvocationExecutionError(
                "agent_permission_denied",
                "Plan requests require an authenticated Assistant session",
            )
        try:
            policy = AgentPolicyService(self.db)
            policy.require_session("assistant", context.agent_session_id)
            policy.require_function("assistant", target.target_id)
            proposal = AgentProposalService(self.db).submit(context, input_json)
        except AgentPermissionError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        except ValueError as exc:
            raise InvocationExecutionError("invalid_input", str(exc)) from None
        return InvocationOutcome(
            status="pending_approval",
            output={"status": proposal.status, "proposal_id": proposal.id},
        )

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> InvocationOutcome:
        raise InvocationExecutionError("invalid_target", "Agent-private targets do not use this approval path")
