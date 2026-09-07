from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.models import InvocationApproval, Skill
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
from app.services.function_registry_service import FunctionRegistryError
from app.services.invocation_approval_contract import (
    InvocationApprovalContractError,
    effective_invocation_contract,
    split_approval_input,
)
from app.services.invocation_approval_service import (
    InvocationApprovalError,
    InvocationApprovalService,
)
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService

from .authorization import (
    AuthorizedIntegrationInvocation,
    IntegrationAuthorizationService,
    _issue_authorized_integration_invocation,
    derive_resource_identity,
)
from .types import IntegrationOperationSpec, ResourceIdentity, RiskLevel


class IntegrationPolicyError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class PendingIntegrationApproval:
    approval_id: int

    @property
    def output(self) -> dict[str, Any]:
        return {"status": "pending_approval", "approval_id": self.approval_id}


class IntegrationCapabilityPolicy:
    """Authorize one current integration invocation without executing it."""

    def __init__(
        self,
        db: Session,
        *,
        registry: Any,
        connection_service: Any,
        project_root: Path | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.connection_service = connection_service
        self.project_root = project_root
        self.proposed_service = ProposedSkillService(db, project_root=project_root)
        self.authorization_service = IntegrationAuthorizationService(db, registry)

    def authorize(
        self,
        context: InvocationContext,
        operation: IntegrationOperationSpec,
        input_json: Mapping[str, Any],
        *,
        approval: InvocationApproval | None = None,
    ) -> AuthorizedIntegrationInvocation | PendingIntegrationApproval:
        current = self.registry.get(operation.id)
        if current is None or current != operation:
            raise IntegrationPolicyError("unknown_operation", "Integration operation does not exist")

        business_input = dict(input_json)
        reason: str | None = None
        if operation.risk is RiskLevel.HIGH and approval is None:
            try:
                effective = effective_invocation_contract(
                    description=operation.description,
                    input_schema=operation.input_schema,
                    output_schema=operation.output_schema,
                    requires_invocation_approval=True,
                )
                self._validate_json(business_input, effective.input_schema)
                reason, business_input = split_approval_input(business_input)
            except InvocationApprovalContractError as exc:
                raise IntegrationPolicyError("invalid_input", f"Integration input is invalid: {exc}") from None
        self._validate_json(business_input, operation.input_schema)

        resource = derive_resource_identity(operation, business_input)
        authorization_id = self._authorize_caller(context, operation, resource)
        account_id = self._require_current_connection(operation)

        if approval is not None:
            self._verify_claimed_approval(
                approval,
                context=context,
                operation=operation,
                input_json=business_input,
                account_id=account_id,
                resource=resource,
            )
        elif operation.risk is RiskLevel.HIGH:
            assert reason is not None
            return self._submit_approval(
                context,
                operation,
                business_input,
                reason,
                account_id,
                resource,
            )

        return _issue_authorized_integration_invocation(
            context=context,
            operation=operation,
            input_json=business_input,
            provider_account_id=account_id,
            resource=resource,
            authorization_id=authorization_id,
        )

    def operation_contract_fingerprint(self, operation: IntegrationOperationSpec) -> str:
        return _fingerprint({"id": operation.id, "contract": operation.contract_identity()})

    def _authorize_caller(
        self,
        context: InvocationContext,
        operation: IntegrationOperationSpec,
        resource: ResourceIdentity | None,
    ) -> int | None:
        if context.principal_kind == "agent":
            if context.agent_id is None or context.agent_session_id is None:
                raise IntegrationPolicyError("agent_permission_denied", "Agent authority is incomplete")
            try:
                policy = AgentPolicyService(self.db)
                policy.require_session(context.agent_id, context.agent_session_id)
                policy.require_function(context.agent_id, operation.id)
            except AgentPermissionError as exc:
                raise IntegrationPolicyError(exc.error_type, str(exc)) from None
            return None

        if context.principal_kind not in {"skill", "web_app"}:
            return None

        if context.principal_kind == "web_app":
            try:
                InvocationContextFactory(
                    self.db,
                    project_root=self.project_root,
                ).require_current_web_app(context)
            except FunctionRegistryError as exc:
                raise IntegrationPolicyError("authorization_missing_or_stale", str(exc)) from None

        skill = self.db.get(Skill, context.caller_skill_id)
        if skill is None or context.caller_version_id is None:
            raise IntegrationPolicyError(
                "authorization_missing_or_stale",
                "Integration caller no longer exists",
            )
        if (
            skill.status != "installed"
            or not skill.enabled
            or skill.active_version_id != context.caller_version_id
            or skill.runtime != context.caller_runtime
            or context.caller_runtime not in {"function", "web_app", "service"}
        ):
            raise IntegrationPolicyError(
                "authorization_missing_or_stale",
                "Integration caller is no longer installed, enabled, and current",
            )
        if skill.runtime == "service" and context.source_schedule_id is None:
            raise IntegrationPolicyError(
                "authorization_missing_or_stale",
                "Service integration caller is not schedule-attributed",
            )
        try:
            manifest = validate_manifest_file(
                self.proposed_service.skill_dir_for_record(skill) / "manifest.json"
            )
        except (ManifestValidationError, ProposedSkillError, FileNotFoundError):
            raise IntegrationPolicyError(
                "authorization_missing_or_stale",
                "Integration caller manifest is invalid",
            ) from None
        runtime_permissions = PermissionService(
            self.db,
            project_root=self.project_root,
        ).can_run(skill, include_integrations=False)
        if not runtime_permissions.allowed:
            raise IntegrationPolicyError(
                "authorization_missing_or_stale",
                "Runtime permission approval is missing or stale",
            )
        requirement = next(
            (item for item in manifest.integration_requirements if operation.id in item.operations),
            None,
        )
        if requirement is None:
            raise IntegrationPolicyError(
                "operation_undeclared",
                "Integration operation is not declared by the active manifest",
            )
        authorization = self.authorization_service.authorization(skill, requirement)
        if authorization is None or authorization.approval_request.status != "approved":
            raise IntegrationPolicyError(
                "authorization_missing_or_stale",
                "Integration authorization is missing or stale",
            )
        scope = self.authorization_service.resource_scope(requirement, operation)
        if not scope.permits(resource):
            error_type = (
                "repository_outside_scope"
                if operation.resource.type == "github.repository"
                else "resource_outside_scope"
            )
            raise IntegrationPolicyError(error_type, "Integration resource is outside the approved scope")
        return authorization.id

    def _require_current_connection(self, operation: IntegrationOperationSpec) -> str | None:
        try:
            available = self.connection_service.operation_available(operation.id)
        except Exception:
            available = False
        if not available:
            if operation.provider_id == "atlas":
                from app.services.atlas_settings_service import AtlasSettingsService

                status = AtlasSettingsService(
                    self.db,
                    secret_store=self.connection_service.secret_store,
                ).status()
                if status.running and status.locked:
                    raise IntegrationPolicyError("atlas_locked", "Atlas is locked")
            raise IntegrationPolicyError(
                "connection_unavailable",
                "Integration connection or configured resource is unavailable",
            )
        connection = self.connection_service.connection(operation.provider_id)
        return connection.account_id if connection is not None else None

    def _submit_approval(
        self,
        context: InvocationContext,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        reason: str,
        account_id: str | None,
        resource: ResourceIdentity | None,
    ) -> PendingIntegrationApproval:
        metadata = {
            "integration_security_v2": {
                "provider": operation.provider_id,
                "risk": operation.risk.value,
                "effects": sorted(effect.value for effect in operation.effects),
                "resource": _resource_value(resource),
            }
        }
        try:
            approval = InvocationApprovalService(
                self.db,
                project_root=self.project_root,
            ).submit_integration(
                operation.id,
                input_json,
                reason,
                target_contract_fingerprint=self.operation_contract_fingerprint(operation),
                provider=operation.provider_id,
                provider_account_id=account_id or "",
                context=context,
                target_description=operation.description,
                dispatch_metadata_json=metadata,
            )
        except InvocationApprovalError as exc:
            raise IntegrationPolicyError(exc.error_type, str(exc)) from None
        return PendingIntegrationApproval(approval.id)

    def _verify_claimed_approval(
        self,
        approval: InvocationApproval,
        *,
        context: InvocationContext,
        operation: IntegrationOperationSpec,
        input_json: Mapping[str, Any],
        account_id: str | None,
        resource: ResourceIdentity | None,
    ) -> None:
        if approval.target_kind != "integration" or approval.target_id != operation.id:
            raise IntegrationPolicyError("stale_contract", "Approved integration target has changed")
        if operation.risk is not RiskLevel.HIGH:
            raise IntegrationPolicyError(
                "stale_contract",
                "Approved integration risk no longer requires invocation approval",
            )
        if self.operation_contract_fingerprint(operation) != approval.target_contract_fingerprint:
            raise IntegrationPolicyError("stale_contract", "Approved integration contract has changed")
        if (account_id or "") != (approval.provider_account_id or ""):
            raise IntegrationPolicyError("connection_changed", "Approved integration account has changed")
        if dict(input_json) != approval.input_json:
            raise IntegrationPolicyError("stale_contract", "Approved integration input has changed")
        security = (approval.dispatch_metadata_json or {}).get("integration_security_v2")
        expected = {
            "provider": operation.provider_id,
            "risk": operation.risk.value,
            "effects": sorted(effect.value for effect in operation.effects),
            "resource": _resource_value(resource),
        }
        if security != expected:
            raise IntegrationPolicyError("stale_contract", "Approved integration security context has changed")
        serialized = (approval.dispatch_metadata_json or {}).get("invocation_context_v1")
        if isinstance(serialized, dict) and InvocationContext.deserialize(serialized) != context:
            raise IntegrationPolicyError("stale_contract", "Approved caller context has changed")

    @staticmethod
    def _validate_json(value: Mapping[str, Any], schema: dict[str, Any]) -> None:
        try:
            Draft202012Validator(schema).validate(dict(value))
        except ValidationError as exc:
            path = ".".join(str(item) for item in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise IntegrationPolicyError(
                "invalid_input",
                f"Integration input is invalid{location}",
            ) from None


def _resource_value(resource: ResourceIdentity | None) -> dict[str, Any] | None:
    if resource is None:
        return None
    return {
        "type": resource.type,
        "values": {key: value for key, value in sorted(resource.values.items())},
    }


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
