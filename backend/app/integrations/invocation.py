from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.models import IntegrationAuditRecord, InvocationApproval, Skill
from app.models.entities import utc_now

from .connections import IntegrationConnectionService
from .policy import (
    IntegrationCapabilityPolicy,
    IntegrationPolicyError,
    PendingIntegrationApproval,
)
from .providers import build_provider_adapters
from .registry import DEFAULT_INTEGRATION_REGISTRY, IntegrationOperationRegistry
from .runtime import IntegrationRuntime, IntegrationRuntimeError


class IntegrationInvocationError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class IntegrationInvocationResult:
    output: dict[str, Any]
    audit_resource: str | None = None


class IntegrationInvocationService:
    """Provider-neutral catalog integration orchestration.

    The compatibility service supplies connection/provider dependencies only;
    it is not consulted as an invocation authorization authority.
    """

    def __init__(
        self,
        db: Session,
        *,
        compatibility_service: Any,
        project_root: Path | None = None,
        registry: IntegrationOperationRegistry = DEFAULT_INTEGRATION_REGISTRY,
        runtime: IntegrationRuntime | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.compatibility_service = compatibility_service
        self.connection_service = IntegrationConnectionService(compatibility_service)
        self.policy = IntegrationCapabilityPolicy(
            db,
            registry=registry,
            connection_service=self.connection_service,
            project_root=project_root,
        )
        self.runtime = runtime or IntegrationRuntime(
            registry=registry,
            adapters=build_provider_adapters(compatibility_service),
        )

    def execute(
        self,
        context: InvocationContext,
        operation_id: str,
        input_json: dict[str, Any],
    ) -> IntegrationInvocationResult:
        operation = self.registry.get(operation_id)
        if operation is None:
            raise IntegrationInvocationError("unknown_operation", "Integration operation does not exist")
        audit = self._start_audit(context, operation_id, input_json)
        try:
            decision = self.policy.authorize(context, operation, input_json)
            if isinstance(decision, PendingIntegrationApproval):
                result = IntegrationInvocationResult(output=decision.output)
            else:
                provider_result = self.runtime.execute(decision)
                result = IntegrationInvocationResult(
                    output=provider_result.output,
                    audit_resource=provider_result.audit_resource,
                )
            self._complete_audit(audit, result)
            return result
        except (IntegrationPolicyError, IntegrationRuntimeError) as exc:
            self._fail_audit(audit, exc.error_type)
            raise IntegrationInvocationError(exc.error_type, str(exc)) from None
        except Exception:
            self._fail_audit(audit, "internal_failure")
            raise IntegrationInvocationError(
                "internal_failure",
                "Integration failed safely",
            ) from None

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> IntegrationInvocationResult:
        operation = self.registry.get(approval.target_id)
        if operation is None:
            raise IntegrationInvocationError(
                "stale_contract",
                "Approved integration contract is no longer current",
            )
        try:
            invocation = self.policy.authorize(
                context,
                operation,
                approval.input_json,
                approval=approval,
            )
            if isinstance(invocation, PendingIntegrationApproval):
                raise IntegrationPolicyError(
                    "stale_contract",
                    "Approved integration unexpectedly requested another approval",
                )
            provider_result = self.runtime.execute(invocation)
            return IntegrationInvocationResult(
                output=provider_result.output,
                audit_resource=provider_result.audit_resource,
            )
        except (IntegrationPolicyError, IntegrationRuntimeError) as exc:
            raise IntegrationInvocationError(exc.error_type, str(exc)) from None

    def _start_audit(
        self,
        context: InvocationContext,
        operation_id: str,
        input_json: dict[str, Any],
    ) -> IntegrationAuditRecord | None:
        if context.principal_kind not in {"skill", "web_app"}:
            return None
        skill = self.db.get(Skill, context.caller_skill_id)
        if skill is None or context.caller_version_id is None:
            raise IntegrationInvocationError(
                "authorization_missing_or_stale",
                "Integration caller no longer exists",
            )
        audit = IntegrationAuditRecord(
            skill_id=skill.id,
            version_id=context.caller_version_id,
            skill_run_id=context.caller_run_id,
            web_app_instance_id=context.web_app_instance_id,
            operation_id=operation_id,
            status="running",
            request_size=len(json.dumps(input_json, separators=(",", ":")).encode("utf-8")),
            started_at=utc_now(),
        )
        self.db.add(audit)
        self._commit_audit()
        return audit

    def _complete_audit(
        self,
        audit: IntegrationAuditRecord | None,
        result: IntegrationInvocationResult,
    ) -> None:
        if audit is None:
            return
        audit.status = "succeeded"
        audit.resource = result.audit_resource
        audit.response_size = len(
            json.dumps(result.output, separators=(",", ":")).encode("utf-8")
        )
        audit.completed_at = utc_now()
        self._commit_audit()

    def _fail_audit(self, audit: IntegrationAuditRecord | None, error_type: str) -> None:
        if audit is None:
            return
        audit.status = "failed"
        audit.error_type = error_type
        audit.completed_at = utc_now()
        self._commit_audit()

    def _commit_audit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationInvocationError(
                "internal_failure",
                "Integration audit failed safely",
            ) from None

