from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.models import IntegrationAuditRecord, InvocationApproval, Skill
from app.models.entities import utc_now
from app.services.email_contracts import (
    MAX_AGGREGATE_RESULT_BYTES,
    EmailContractError,
    decode_composite_cursor,
    encode_composite_cursor,
    enforce_budget,
    provider_error,
    timestamp_key,
    validate_provider,
    validate_providers,
)

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
    def __init__(self, error_type: str, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.provider = provider


@dataclass(frozen=True)
class IntegrationInvocationResult:
    output: dict[str, Any] | list[Any]
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
        providers = self._selected_providers(operation, input_json)
        if len(providers) == 1 and operation.provider_selection.value == "single":
            return self._execute_single(context, operation, input_json, providers[0])
        return self._execute_multi(context, operation, input_json, providers)

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
            provider_id = self._selected_providers(operation, approval.input_json)[0]
            audit = self._start_audit(context, operation.id, approval.input_json, provider_id=provider_id)
            invocation = self.policy.authorize(
                context,
                operation,
                approval.input_json,
                provider_id=provider_id,
                approval=approval,
            )
            if isinstance(invocation, PendingIntegrationApproval):
                raise IntegrationPolicyError(
                    "stale_contract",
                    "Approved integration unexpectedly requested another approval",
                )
            provider_result = self.runtime.execute(invocation)
            self._validate_public_output(operation, provider_result.output)
            result = IntegrationInvocationResult(
                output=provider_result.output,
                audit_resource=provider_result.audit_resource,
            )
            self._complete_audit(audit, result, invocation=invocation)
            return result
        except (IntegrationPolicyError, IntegrationRuntimeError) as exc:
            self._fail_audit(locals().get("audit"), exc.error_type)
            raise IntegrationInvocationError(exc.error_type, str(exc), provider=locals().get("provider_id")) from None
        except IntegrationInvocationError:
            self._fail_audit(locals().get("audit"), "invalid_input")
            raise
        except Exception:
            self._fail_audit(locals().get("audit"), "internal_failure")
            raise IntegrationInvocationError(
                "internal_failure",
                "Integration failed safely",
                provider=locals().get("provider_id"),
            ) from None

    def _execute_single(
        self,
        context: InvocationContext,
        operation: Any,
        input_json: dict[str, Any],
        provider_id: str,
    ) -> IntegrationInvocationResult:
        audit = self._start_audit(context, operation.id, input_json, provider_id=provider_id)
        try:
            decision = self.policy.authorize(
                context,
                operation,
                input_json,
                provider_id=provider_id,
            )
            if isinstance(decision, PendingIntegrationApproval):
                result = IntegrationInvocationResult(output=decision.output)
                self._complete_audit(audit, result)
                return result
            provider_result = self.runtime.execute(decision)
            self._validate_public_output(operation, provider_result.output)
            result = IntegrationInvocationResult(
                output=provider_result.output,
                audit_resource=provider_result.audit_resource,
            )
            self._complete_audit(audit, result, invocation=decision)
            return result
        except (IntegrationPolicyError, IntegrationRuntimeError) as exc:
            self._fail_audit(audit, exc.error_type)
            raise IntegrationInvocationError(exc.error_type, str(exc), provider=provider_id) from None
        except IntegrationInvocationError:
            self._fail_audit(audit, "invalid_input")
            raise
        except Exception:
            self._fail_audit(audit, "internal_failure")
            raise IntegrationInvocationError("internal_failure", "Integration failed safely", provider=provider_id) from None

    def _execute_multi(
        self,
        context: InvocationContext,
        operation: Any,
        input_json: dict[str, Any],
        providers: tuple[str, ...],
    ) -> IntegrationInvocationResult:
        if operation.id.startswith("email."):
            try:
                Draft202012Validator(operation.input_schema).validate(input_json)
            except ValidationError:
                raise IntegrationInvocationError(
                    "invalid_input",
                    "Integration input is invalid",
                ) from None
        try:
            cursors = (
                decode_composite_cursor(input_json.get("page_token"), providers)
                if operation.id == "email.search"
                else {}
            )
        except EmailContractError as exc:
            raise IntegrationInvocationError("invalid_input", str(exc)) from None
        slices: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []
        audit_resources: list[str] = []
        for provider_id in providers:
            audit = self._start_audit(context, operation.id, input_json, provider_id=provider_id)
            provider_input = dict(input_json)
            if operation.id == "email.search":
                provider_input["page_token"] = cursors.get(provider_id)
            try:
                decision = self.policy.authorize(
                    context,
                    operation,
                    provider_input,
                    provider_id=provider_id,
                )
                if isinstance(decision, PendingIntegrationApproval):
                    raise IntegrationPolicyError("approval_required", "Provider operation requires approval")
                provider_result = self.runtime.execute(decision)
                slices[provider_id] = provider_result.output
                resource = provider_result.audit_resource
                if resource:
                    audit_resources.append(resource)
                self._complete_audit(audit, IntegrationInvocationResult(provider_result.output, resource), invocation=decision)
            except (IntegrationPolicyError, IntegrationRuntimeError) as exc:
                if exc.error_type == "invalid_input":
                    self._fail_audit(audit, exc.error_type)
                    raise IntegrationInvocationError(exc.error_type, str(exc)) from None
                errors.append(provider_error(provider_id, exc.error_type, str(exc), getattr(exc, "retry_after_seconds", None)))
                self._fail_audit(audit, exc.error_type)
            except Exception:
                errors.append(provider_error(provider_id, "internal_failure", "Email provider failed safely"))
                self._fail_audit(audit, "internal_failure")
        try:
            output = self._merge_email_results(operation, providers, slices, errors, cursors)
        except EmailContractError as exc:
            raise IntegrationInvocationError("invalid_input", str(exc)) from None
        self._validate_public_output(operation, output)
        result = IntegrationInvocationResult(output=output, audit_resource=", ".join(audit_resources) or None)
        return result

    def _selected_providers(self, operation: Any, input_json: dict[str, Any]) -> tuple[str, ...]:
        supported = self.registry.operation_provider_set(operation.id)
        if operation.provider_selection.value == "multi":
            try:
                selected = validate_providers(input_json.get("providers"))
            except ValueError as exc:
                raise IntegrationInvocationError("invalid_input", str(exc)) from None
            if any(not self.registry.supports_provider(operation.id, provider) for provider in selected):
                raise IntegrationInvocationError("invalid_input", "Requested provider is not supported by the operation")
            return selected
        if "provider" in input_json:
            try:
                selected = validate_provider(input_json.get("provider"))
            except ValueError as exc:
                raise IntegrationInvocationError("invalid_input", str(exc)) from None
            if not self.registry.supports_provider(operation.id, selected):
                raise IntegrationInvocationError("invalid_input", "Requested provider is not supported by the operation")
            return (selected,)
        if len(supported) == 1:
            return (supported[0],)
        raise IntegrationInvocationError("invalid_input", "provider is required")

    def _merge_email_results(
        self,
        operation: Any,
        providers: tuple[str, ...],
        slices: dict[str, dict[str, Any]],
        errors: list[dict[str, Any]],
        cursors: dict[str, Any],
    ) -> dict[str, Any]:
        if operation.id == "email.search":
            conversations = [item for value in slices.values() for item in value.get("conversations", [])]
            conversations.sort(key=lambda item: timestamp_key(item["latest_timestamp"]), reverse=True)
            next_cursors = {provider: slices.get(provider, {}).get("next_page_token", cursors.get(provider)) for provider in providers}
            has_more = any(bool(slices.get(provider, {}).get("has_more")) for provider in providers)
            return {
                "conversations": conversations[:50],
                "has_more": has_more,
                "next_page_token": encode_composite_cursor(next_cursors, providers) if has_more else None,
                "provider_errors": errors,
            }
        messages = [item for value in slices.values() for item in value.get("messages", [])]
        messages.sort(key=lambda item: timestamp_key(item["timestamp"]), reverse=True)
        output: dict[str, Any] = {
            "messages": messages[:100],
            "count": len(messages[:100]),
            "has_more": any(bool(value.get("has_more")) for value in slices.values()),
            "provider_errors": [*errors, *[error for value in slices.values() for error in value.get("provider_errors", [])]],
        }
        if operation.id == "email.read_and_mark_new":
            output["mark_outcomes"] = [
                *[outcome for value in slices.values() for outcome in value.get("mark_outcomes", [])],
                *[
                    {
                        "provider": provider,
                        "marked_message_ids": [],
                        "marked_count": 0,
                        "failed_message_ids": [],
                        "failed_count": 0,
                        "unknown_message_ids": [],
                        "unknown_count": 0,
                    }
                    for provider in providers
                    if provider not in {outcome.get("provider") for value in slices.values() for outcome in value.get("mark_outcomes", [])}
                ],
            ]
        enforce_budget(output, maximum=MAX_AGGREGATE_RESULT_BYTES)
        return output

    def _validate_public_output(self, operation: Any, output: dict[str, Any] | list[Any]) -> None:
        try:
            Draft202012Validator(operation.output_schema).validate(output)
        except ValidationError:
            raise IntegrationInvocationError("internal_failure", "Integration returned an invalid normalized result") from None

    def _start_audit(
        self,
        context: InvocationContext,
        operation_id: str,
        input_json: dict[str, Any],
        *,
        provider_id: str | None = None,
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
            provider=provider_id,
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
        invocation: Any | None = None,
    ) -> None:
        if audit is None:
            return
        audit.status = "succeeded"
        audit.resource = result.audit_resource
        if invocation is not None:
            audit.connection_id = invocation.connection_id
            audit.account_id = invocation.provider_account_id
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
