from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.integrations.invocation import IntegrationInvocationError, IntegrationInvocationService
from app.models import InvocationApproval
from app.services.integration_service import IntegrationService, build_default_integration_service


class IntegrationHandler:
    def __init__(self, db: Session, *, project_root=None, service: IntegrationService | None = None) -> None:
        self.db = db
        self.project_root = project_root
        self.service = service or build_default_integration_service(db)
        self.invocations = IntegrationInvocationService(
            db,
            compatibility_service=self.service,
            project_root=project_root,
        )

    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        try:
            result = self.invocations.execute(context, target.target_id, input_json)
        except IntegrationInvocationError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        approval_id = None
        status = "succeeded"
        if isinstance(result.output, dict) and result.output.get("status") == "pending_approval":
            status = "pending_approval"
            approval_id = result.output.get("approval_id")
        return InvocationOutcome(
            status=status,
            output=result.output,
            approval_id=approval_id,
            audit_resource=result.audit_resource,
        )

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if approval.target_kind != "integration":
            raise InvocationExecutionError("invalid_target", "Approval is not for an integration")
        try:
            result = self.invocations.execute_approved(approval, context)
        except IntegrationInvocationError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        return InvocationOutcome(
            status="succeeded",
            output=result.output,
            audit_resource=result.audit_resource,
        )
