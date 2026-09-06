from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.models import InvocationApproval
from app.services.function_registry_service import FunctionRegistryError
from app.services.integration_service import IntegrationError, IntegrationService, build_default_integration_service


class IntegrationHandler:
    def __init__(self, db: Session, *, project_root=None, service: IntegrationService | None = None) -> None:
        self.db = db
        self.project_root = project_root
        self.service = service or build_default_integration_service(db)

    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        try:
            result = self.service.execute_context(context, target.target_id, input_json)
        except IntegrationError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        approval_id = None
        status = "succeeded"
        if result.output.get("status") == "pending_approval":
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
        if context.principal_kind == "web_app":
            try:
                InvocationContextFactory(self.db, project_root=self.project_root).require_current_web_app(context)
            except FunctionRegistryError as exc:
                raise InvocationExecutionError("authorization_missing_or_stale", str(exc)) from None
        try:
            result = self.service.execute_claimed_approval(approval, context)
        except IntegrationError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        return InvocationOutcome(
            status="succeeded",
            output=result.output,
            audit_resource=result.audit_resource,
        )
