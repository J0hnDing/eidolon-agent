from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.models import InvocationApproval, Skill
from app.services.invocation_approval_contract import (
    MAX_INVOCATION_APPROVAL_INPUT_BYTES,
    effective_invocation_contract,
    pending_approval_receipt,
    split_approval_input,
)
from app.services.invocation_approval_presentation import (
    build_approval_presentation,
    presentation_snapshot,
)
from app.services.manifest_validator import validate_manifest_file
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_graph_service import SkillGraphService


class InvocationApprovalError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class InvocationApprovalService:
    def __init__(self, db: Session, *, project_root=None) -> None:
        self.db = db
        self.proposed_service = ProposedSkillService(db, project_root=project_root)
        self.project_root = self.proposed_service.project_root

    def list(
        self,
        *,
        decision_status: str | None = None,
        execution_status: str | None = None,
    ) -> list[InvocationApproval]:
        query = select(InvocationApproval)
        if decision_status is not None:
            query = query.where(InvocationApproval.decision_status == decision_status)
        if execution_status is not None:
            query = query.where(InvocationApproval.execution_status == execution_status)
        return list(
            self.db.scalars(
                query.order_by(InvocationApproval.created_at.desc(), InvocationApproval.id.desc())
            ).all()
        )

    def get(self, approval_id: int) -> InvocationApproval:
        approval = self.db.get(InvocationApproval, approval_id)
        if approval is None:
            raise InvocationApprovalError("not_found", "Invocation approval was not found")
        return approval

    def submit_user_function(
        self,
        target: Skill,
        input_json: dict[str, Any],
        *,
        context: InvocationContext,
    ) -> InvocationApproval:
        from app.services.function_registry_service import FunctionRegistryService

        manifest = validate_manifest_file(
            self.proposed_service.skill_dir_for_record(target) / "manifest.json"
        )
        graph = SkillGraphService(
            self.db,
            project_root=self.project_root,
        ).effective_contract(target, manifest=manifest)
        if manifest.runtime != "function" or not (
            manifest.requires_invocation_approval or graph.risk_level == "high"
        ):
            raise InvocationApprovalError(
                "approval_not_required", "The target does not require per-call approval"
            )
        if manifest.input_schema is None or manifest.output_schema is None:
            raise InvocationApprovalError("invalid_contract", "The function callable contract is incomplete")
        effective = effective_invocation_contract(
            description=manifest.description,
            input_schema=manifest.input_schema,
            output_schema=manifest.output_schema,
            requires_invocation_approval=True,
        )
        self._validate_json(input_json, effective.input_schema, "approval-required function input")
        reason, business_input = split_approval_input(input_json)
        self._validate_json(business_input, manifest.input_schema, "function input")
        self._require_bounded_input(input_json)
        self._require_telegram()
        registry = FunctionRegistryService(self.db, project_root=self.project_root)
        approval = InvocationApproval(
            target_kind="user_function",
            target_id=target.name,
            target_skill_id=target.id,
            target_version_id=target.active_version_id,
            target_contract_fingerprint=registry.target_contract_fingerprint(target),
            target_description=manifest.description,
            caller_type=context.approval_caller_type(),
            source=context.approval_source("user"),
            caller_skill_id=context.caller_skill_id,
            caller_version_id=context.caller_version_id,
            caller_run_id=context.caller_run_id,
            web_app_instance_id=context.web_app_instance_id,
            initiating_action=context.initiating_action,
            input_json=business_input,
            input_hash=self._input_hash(business_input),
            reason_to_call=reason,
            presentation_json=presentation_snapshot(
                build_approval_presentation(
                    approval_id=0,
                    action=target.name,
                    caller=self._presentation_caller(context, "user"),
                    input_json=business_input,
                    reason=reason,
                )
            ),
            dispatch_metadata_json={"invocation_context_v1": context.serialize()},
        )
        return self._commit_and_deliver(approval)

    def submit_integration(
        self,
        operation_id: str,
        business_input: dict[str, Any],
        reason_to_call: str,
        *,
        target_contract_fingerprint: str,
        provider: str,
        provider_account_id: str,
        context: InvocationContext,
        target_description: str = "",
        dispatch_metadata_json: dict[str, Any] | None = None,
    ) -> InvocationApproval:
        normalized_reason = reason_to_call.strip()
        if not normalized_reason or len(normalized_reason) > 500:
            raise InvocationApprovalError(
                "invalid_input", "reason_to_call must contain between 1 and 500 characters"
            )
        self._require_bounded_input({**business_input, "reason_to_call": normalized_reason})
        self._require_telegram()
        approval = InvocationApproval(
            target_kind="integration",
            target_id=operation_id,
            target_contract_fingerprint=target_contract_fingerprint,
            target_description=target_description or operation_id,
            provider=provider,
            provider_account_id=provider_account_id,
            caller_type=context.approval_caller_type(),
            source=context.approval_source("integration"),
            caller_skill_id=context.caller_skill_id,
            caller_version_id=context.caller_version_id,
            caller_run_id=context.caller_run_id,
            web_app_instance_id=context.web_app_instance_id,
            initiating_action=context.initiating_action,
            input_json=business_input,
            input_hash=self._input_hash(business_input),
            reason_to_call=normalized_reason,
            presentation_json=presentation_snapshot(
                build_approval_presentation(
                    approval_id=0,
                    action=operation_id,
                    caller=self._presentation_caller(context, "integration"),
                    input_json=business_input,
                    reason=normalized_reason,
                )
            ),
            dispatch_metadata_json={
                **(dispatch_metadata_json or {}),
                "invocation_context_v1": context.serialize(),
            },
        )
        return self._commit_and_deliver(approval)

    def approve(
        self,
        approval_id: int,
        *,
        decided_via: str,
        decided_by: str,
    ) -> InvocationApproval:
        approval = self.get(approval_id)
        if approval.decision_status == "denied" or approval.execution_status != "not_started":
            return approval
        from app.models.entities import utc_now

        claimed = self.db.execute(
            update(InvocationApproval)
            .where(InvocationApproval.id == approval_id)
            .where(InvocationApproval.decision_status.in_(("pending", "approved")))
            .where(InvocationApproval.execution_status == "not_started")
            .values(
                decision_status="approved",
                execution_status="executing",
                decided_via=decided_via[:32],
                decided_by=decided_by[:128],
                decided_at=utc_now(),
                execution_started_at=utc_now(),
            )
        )
        self.db.commit()
        self.db.expire_all()
        approval = self.get(approval_id)
        if claimed.rowcount != 1:
            return approval
        approval = self._dispatch_claimed(approval)
        self._update_telegram_outcome(approval)
        return self.get(approval.id)

    def deny(
        self,
        approval_id: int,
        *,
        decided_via: str,
        decided_by: str,
    ) -> InvocationApproval:
        approval = self.get(approval_id)
        if approval.decision_status != "pending":
            return approval
        from app.models.entities import utc_now

        self.db.execute(
            update(InvocationApproval)
            .where(InvocationApproval.id == approval_id)
            .where(InvocationApproval.decision_status == "pending")
            .where(InvocationApproval.execution_status == "not_started")
            .values(
                decision_status="denied",
                decided_via=decided_via[:32],
                decided_by=decided_by[:128],
                decided_at=utc_now(),
            )
        )
        self.db.commit()
        self.db.expire_all()
        approval = self.get(approval_id)
        self._update_telegram_outcome(approval)
        return self.get(approval_id)

    def recover(self) -> list[InvocationApproval]:
        """Recover safe queued work and never replay an interrupted side effect."""
        from app.models.entities import utc_now

        self.db.execute(
            update(InvocationApproval)
            .where(InvocationApproval.execution_status == "executing")
            .values(
                execution_status="outcome_unknown",
                error_type="interrupted_execution",
                error_message=(
                    "Eidolon restarted while this action was executing; the external outcome is unknown."
                ),
                execution_completed_at=utc_now(),
            )
        )
        self.db.commit()
        queued = self.db.scalars(
            select(InvocationApproval)
            .where(InvocationApproval.decision_status == "approved")
            .where(InvocationApproval.execution_status == "not_started")
            .order_by(InvocationApproval.id)
        ).all()
        recovered: list[InvocationApproval] = []
        for approval in queued:
            recovered.append(
                self.approve(
                    approval.id,
                    decided_via=approval.decided_via or "recovery",
                    decided_by=approval.decided_by or "eidolon",
                )
            )
        return recovered

    def approval_available(self) -> bool:
        try:
            return bool(self._telegram_service().approval_available())
        except Exception:
            return False

    @staticmethod
    def receipt(approval: InvocationApproval) -> dict[str, Any]:
        return pending_approval_receipt(approval.id)

    def _dispatch_claimed(self, approval: InvocationApproval) -> InvocationApproval:
        from app.models.entities import utc_now

        try:
            from app.execution.executor import InvocationExecutor

            outcome = InvocationExecutor(
                self.db,
                project_root=self.project_root,
            ).execute_approved(approval)
            if outcome.output is None:
                raise InvocationApprovalError(
                    outcome.error_type or "execution_failed",
                    outcome.error_message or "Approved invocation returned no output",
                )
            approval.result_json = outcome.output
            approval.execution_status = "succeeded"
            approval.execution_completed_at = utc_now()
            self.db.commit()
            self.db.refresh(approval)
            return approval
        except Exception as exc:
            self.db.rollback()
            approval = self.get(approval.id)
            error_type = str(getattr(exc, "error_type", "execution_failed"))[:64]
            if error_type in {
                "stale_contract",
                "connection_changed",
                "authorization_missing_or_stale",
                "function_unavailable",
            }:
                approval.execution_status = "stale"
            else:
                approval.execution_status = "failed"
            approval.error_type = error_type
            approval.error_message = str(exc)[:2000]
            approval.execution_completed_at = utc_now()
            self.db.commit()
            self.db.refresh(approval)
            return approval

    def _commit_and_deliver(self, approval: InvocationApproval) -> InvocationApproval:
        self.db.add(approval)
        self.db.commit()
        self.db.refresh(approval)
        try:
            telegram = self._telegram_service()
            telegram.deliver_invocation_approval(approval)
            self.db.refresh(approval)
        except Exception as exc:
            self.db.rollback()
            approval = self.get(approval.id)
            approval.telegram_delivery_status = "failed"
            approval.error_type = "telegram_delivery_failed"
            approval.error_message = str(exc)[:1000]
            self.db.commit()
            self.db.refresh(approval)
        return approval

    @staticmethod
    def _presentation_caller(context: InvocationContext, category: str) -> str:
        if context.principal_kind == "agent":
            return f"{str(context.agent_id).title()} session {context.agent_session_id}"
        return context.approval_source(category)

    def _update_telegram_outcome(self, approval: InvocationApproval) -> None:
        try:
            self._telegram_service().update_invocation_approval(approval)
        except Exception:
            self.db.rollback()
            approval = self.get(approval.id)
            if approval.telegram_message_ids_json:
                approval.telegram_delivery_status = "update_failed"
                self.db.commit()

    def _require_telegram(self) -> None:
        if not self.approval_available():
            raise InvocationApprovalError(
                "telegram_unavailable",
                "A paired Telegram notification and approval bot is required for this function",
            )

    def _telegram_service(self):
        from app.services.telegram_service import TelegramService

        return TelegramService(self.db)

    @staticmethod
    def _validate_json(value: dict[str, Any], schema: dict[str, Any], label: str) -> None:
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError as exc:
            path = ".".join(str(item) for item in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise InvocationApprovalError(
                "invalid_input", f"Invalid {label}{location}: {exc.message}"
            ) from None

    @staticmethod
    def _require_bounded_input(value: dict[str, Any]) -> None:
        size = len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if size > MAX_INVOCATION_APPROVAL_INPUT_BYTES:
            raise InvocationApprovalError(
                "request_too_large",
                f"Approval-required input exceeds {MAX_INVOCATION_APPROVAL_INPUT_BYTES} bytes",
            )

    @staticmethod
    def _input_hash(value: dict[str, Any]) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
