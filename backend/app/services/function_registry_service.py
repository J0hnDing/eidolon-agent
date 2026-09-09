import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.skill_context import skill_execution_context
from app.models import (
    ApprovalRequest,
    FunctionAccessApproval,
    InvocationApproval,
    Skill,
    SkillRun,
    SkillVersion,
    WebAppInstance,
)
from app.schemas.function_registry import FunctionContractRead, FunctionRequirementReview
from app.schemas.manifest import SkillManifest
from app.services.invocation_approval_contract import effective_invocation_contract
from app.services.invocation_approval_service import (
    InvocationApprovalError,
    InvocationApprovalService,
)
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.skill_graph_service import SkillGraphService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import FunctionRunContext, get_skill_runner, validate_supported_permissions


class FunctionRegistryError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class FunctionRegistryService:
    db: Session
    project_root: Path | None = None
    runner_factory: Callable[[Session], Any] = get_skill_runner

    def __post_init__(self) -> None:
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)
        self.project_root = self.proposed_service.project_root

    def list_contracts(self, caller: Skill | None = None) -> list[FunctionContractRead]:
        skills = self.db.scalars(
            select(Skill)
            .where(Skill.status == "installed")
            .where(Skill.runtime == "function")
            .order_by(Skill.name)
        ).all()
        requirements = self._requirements_by_name(caller) if caller is not None else set()
        return [
            self.contract_for_skill(
                skill,
                declared_by_caller=skill.name in requirements if caller is not None else None,
                access_state=self._access_state(caller, skill) if caller is not None else "not_requested",
            )
            for skill in skills
        ]

    def discovery_context(self) -> list[dict[str, Any]]:
        return [
            {
                "name": contract.name,
                "description": contract.description,
                "active_version": contract.active_version,
                "input_schema": contract.input_schema,
                "output_schema": contract.output_schema,
                "risk_level": contract.risk_level,
                "availability": contract.availability,
            }
            for contract in self.list_contracts()
        ]

    def contract_for_skill(
        self,
        skill: Skill,
        *,
        declared_by_caller: bool | None = None,
        access_state: str = "not_requested",
    ) -> FunctionContractRead:
        reasons: list[str] = []
        manifest: SkillManifest | None = None
        active_version = self.db.get(SkillVersion, skill.active_version_id) if skill.active_version_id else None
        if skill.status != "installed":
            reasons.append("Function is not installed")
        if skill.runtime != "function":
            reasons.append("Target is not a function runtime")
        if not skill.enabled:
            reasons.append("Function is disabled")
        if active_version is None or active_version.status != "active":
            reasons.append("Function has no active installed version")
        try:
            manifest = self._active_manifest(skill)
        except (FileNotFoundError, ManifestValidationError, ProposedSkillError) as exc:
            reasons.append(f"Active manifest is invalid: {exc}")
        if manifest is not None:
            if manifest.runtime != "function":
                reasons.append("Active manifest is not a function runtime")
            if manifest.name != skill.name:
                reasons.append("Active manifest identity does not match the registry record")
            if manifest.input_schema is None:
                reasons.append("Function input_schema is required for registry invocation")
            if manifest.output_schema is None:
                reasons.append("Function output_schema is required for registry invocation")
            try:
                validate_supported_permissions(manifest)
            except ValueError as exc:
                reasons.append(str(exc))
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_run(skill)
        if not permission_decision.allowed:
            reasons.append(permission_decision.reason)
        graph_contract = SkillGraphService(
            self.db,
            project_root=self.project_root,
        ).effective_contract(
            skill,
            manifest=manifest,
        )
        reasons.extend(graph_contract.availability_reasons)
        reasons = list(dict.fromkeys(reasons))
        availability = "available"
        if graph_contract.availability == "error":
            availability = "error"
        elif not skill.enabled and all(reason == "Function is disabled" for reason in reasons):
            availability = "disabled"
        elif reasons:
            availability = (
                "disabled"
                if graph_contract.availability == "disabled"
                or reasons == ["Function is disabled"]
                else "unavailable"
            )
        permissions = graph_contract.permissions
        risk_level = graph_contract.risk_level
        description = manifest.description if manifest is not None else skill.description
        input_schema = manifest.input_schema if manifest is not None else skill.input_schema_json
        output_schema = manifest.output_schema if manifest is not None else skill.output_schema_json
        requires_invocation_approval = bool(
            manifest is not None
            and (
                manifest.requires_invocation_approval
                or risk_level == "high"
            )
        )
        if (
            manifest is not None
            and input_schema is not None
            and output_schema is not None
        ):
            effective = effective_invocation_contract(
                description=description,
                input_schema=input_schema,
                output_schema=output_schema,
                requires_invocation_approval=requires_invocation_approval,
            )
            description = effective.description
            input_schema = effective.input_schema
            output_schema = effective.output_schema
        return FunctionContractRead(
            skill_id=skill.id,
            name=skill.name,
            description=description,
            active_version_id=skill.active_version_id,
            active_version=active_version.version if active_version is not None else None,
            input_schema=input_schema,
            output_schema=output_schema,
            risk_level=risk_level,
            permissions=permissions,
            availability=availability,
            availability_reasons=reasons,
            declared_by_caller=declared_by_caller,
            access_state=access_state,
            requires_invocation_approval=requires_invocation_approval,
        )

    def review_requirements(
        self,
        caller: Skill,
        *,
        manifest: SkillManifest | None = None,
        create_requests: bool = False,
        caller_version_id: int | None = None,
    ) -> list[FunctionRequirementReview]:
        manifest = manifest or self._manifest_for_caller(caller)
        reviews: list[FunctionRequirementReview] = []
        for function_name in manifest.function_requirements:
            target = self.db.scalar(select(Skill).where(Skill.name == function_name))
            if target is None or target.status != "installed" or target.runtime != "function":
                reviews.append(
                    FunctionRequirementReview(
                        name=function_name,
                        availability_reasons=["Required function is not installed"],
                    )
                )
                continue
            contract = self.contract_for_skill(target)
            approval_required = contract.risk_level in {"medium", "high"}
            approval_request_id: int | None = None
            access_state = "no_approval_required" if not approval_required else self._access_state(caller, target)
            if approval_required and create_requests and access_state in {"not_requested", "stale"}:
                approval = self._create_access_request(
                    caller,
                    target,
                    caller_version_id=caller_version_id,
                )
                approval_request_id = approval.approval_request_id
                access_state = approval.approval_request.status
            elif approval_required:
                approval = self._current_access_approval(caller, target)
                approval_request_id = approval.approval_request_id if approval is not None else None
            if contract.availability != "available" and access_state == "no_approval_required":
                access_state = "unavailable"
            reviews.append(
                FunctionRequirementReview(
                    name=function_name,
                    target_skill_id=target.id,
                    description=contract.description,
                    risk_level=contract.risk_level,
                    availability=contract.availability,
                    availability_reasons=contract.availability_reasons,
                    approval_required=approval_required,
                    access_state=access_state,
                    approval_request_id=approval_request_id,
                )
            )
        return reviews

    def caller_authorization_error(
        self,
        target: Skill,
        context: InvocationContext,
    ) -> str | None:
        if context.principal_kind not in {"skill", "web_app"}:
            return None
        caller = self.db.get(Skill, context.caller_skill_id)
        if (
            caller is None
            or caller.status != "installed"
            or not caller.enabled
            or caller.active_version_id != context.caller_version_id
            or caller.runtime != context.caller_runtime
        ):
            return "Original caller is no longer installed, enabled, and version-current"
        if context.principal_kind == "web_app":
            instance = self.db.get(WebAppInstance, context.web_app_instance_id)
            if (
                caller.runtime != "web_app"
                or instance is None
                or instance.status != "healthy"
                or instance.skill_id != caller.id
                or instance.version_id != context.caller_version_id
            ):
                return "Web application caller is no longer authorized"
        elif caller.runtime not in {"function", "service"}:
            return "Runtime caller is not eligible"
        elif caller.runtime == "service" and context.source_schedule_id is None:
            return "Service caller is no longer schedule-attributed"
        permission = PermissionService(self.db, project_root=self.project_root).can_run(caller)
        if not permission.allowed:
            return permission.reason
        if target.name not in self._requirements_by_name(caller):
            return f"Caller skill {caller.name} did not declare required function {target.name}"
        contract = self.contract_for_skill(target)
        if contract.availability != "available":
            return "; ".join(contract.availability_reasons) or "Target function is unavailable"
        if contract.risk_level in {"medium", "high"} and self._access_state(caller, target) != "approved":
            return f"Caller-specific approval is required for {caller.name} to invoke {target.name}"
        return None

    def execute_resolved(
        self,
        target: Skill,
        input_json: dict[str, Any],
        context: InvocationContext,
    ) -> SkillRun | InvocationApproval:
        source = context.function_source()
        contract = self.contract_for_skill(target)
        availability_reasons = list(contract.availability_reasons)
        if source in {"direct_user", "backend", "schedule"}:
            availability_reasons = [
                reason
                for reason in availability_reasons
                if reason
                not in {
                    "Function has no active installed version",
                    "Function input_schema is required for registry invocation",
                    "Function output_schema is required for registry invocation",
                }
            ]
        if availability_reasons:
            return self.blocked_run_for_context(
                target,
                input_json,
                "; ".join(availability_reasons),
                context,
            )
        contract_input = dict(input_json)
        if source == "schedule":
            contract_input.pop("_schedule", None)
        input_error = (
            self._schema_error(contract_input, contract.input_schema, "input")
            if contract.input_schema is not None
            else None
        )
        if input_error:
            return self.blocked_run_for_context(
                target,
                input_json,
                input_error,
                context,
            )
        if contract.requires_invocation_approval:
            try:
                return InvocationApprovalService(
                    self.db, project_root=self.project_root
                ).submit_user_function(
                    target,
                    input_json,
                    context=context,
                )
            except InvocationApprovalError as exc:
                return self.blocked_run_for_context(
                    target,
                    input_json,
                    str(exc),
                    context,
                )
        return self._execute_run(
            target,
            input_json,
            context,
            output_schema=contract.output_schema,
        )

    def _execute_run(
        self,
        target: Skill,
        input_json: dict[str, Any],
        context: InvocationContext,
        *,
        output_schema: dict[str, Any] | None,
    ) -> SkillRun:
        source = context.function_source()
        capability_token = secrets.token_urlsafe(32)
        run_context = FunctionRunContext(
            version_id=target.active_version_id,
            invocation_source=source,
            caller_skill_id=context.caller_skill_id,
            caller_version_id=context.caller_version_id,
            parent_run_id=context.caller_run_id,
            source_schedule_id=context.source_schedule_id,
            web_app_instance_id=context.web_app_instance_id,
            initiating_action=context.initiating_action,
            capability_token=capability_token,
        )
        try:
            with SkillOperationGuard(self.db).locked(
                target,
                "run",
                reason=context.initiating_action or source,
            ):
                runner = self.runner_factory(self.db)
                with skill_execution_context(target.id):
                    try:
                        run = runner.run(
                            skill_id=target.id,
                            skill_dir=self.proposed_service.skill_dir_for_record(target),
                            input_json=input_json,
                            context=run_context,
                        )
                    except TypeError as exc:
                        if "context" not in str(exc):
                            raise
                        run = runner.run(
                            skill_id=target.id,
                            skill_dir=self.proposed_service.skill_dir_for_record(target),
                            input_json=input_json,
                        )
        except SkillOperationConflict as exc:
            return self.blocked_run_for_context(
                target,
                input_json,
                str(exc),
                context,
            )
        if run.output_json is not None and output_schema is not None:
            output_error = self._schema_error(run.output_json, output_schema, "output")
            if output_error:
                run.status = "failed"
                run.error_message = self._merge_error(run.error_message, output_error)
                run.ended_at = run.ended_at or utc_now()
                self.db.commit()
                self.db.refresh(run)
        return run

    def execute_claimed_approval(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> SkillRun:
        if approval.target_kind != "user_function" or approval.target_skill_id is None:
            raise InvocationApprovalError("invalid_target", "Approval is not for a user function")
        target = self.db.get(Skill, approval.target_skill_id)
        if target is None or target.name != approval.target_id:
            raise InvocationApprovalError("function_unavailable", "Approved function is unavailable")
        if target.active_version_id != approval.target_version_id:
            raise InvocationApprovalError("stale_contract", "Approved function version has changed")
        if self.target_contract_fingerprint(target) != approval.target_contract_fingerprint:
            raise InvocationApprovalError("stale_contract", "Approved function contract has changed")
        manifest = self._active_manifest(target)
        graph = SkillGraphService(
            self.db,
            project_root=self.project_root,
        ).effective_contract(target, manifest=manifest)
        if not (manifest.requires_invocation_approval or graph.risk_level == "high"):
            raise InvocationApprovalError("stale_contract", "Function approval requirement has changed")
        caller_error = self.caller_authorization_error(target, context)
        if caller_error is not None:
            raise InvocationApprovalError("stale_contract", caller_error)
        return self._execute_run(
            target,
            approval.input_json,
            context,
            output_schema=manifest.output_schema,
        )

    def blocked_run_for_context(
        self,
        target: Skill,
        input_json: dict[str, Any],
        reason: str,
        context: InvocationContext,
    ) -> SkillRun:
        source = context.function_source()
        run = SkillRun(
            skill_id=target.id,
            version_id=target.active_version_id,
            status="blocked",
            input_json=input_json,
            started_at=utc_now(),
            ended_at=utc_now(),
            error_message=reason,
            invocation_source=source,
            caller_skill_id=context.caller_skill_id,
            caller_version_id=context.caller_version_id,
            parent_run_id=context.caller_run_id,
            source_schedule_id=context.source_schedule_id,
            web_app_instance_id=context.web_app_instance_id,
            initiating_action=context.initiating_action,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def _active_manifest(self, skill: Skill) -> SkillManifest:
        return validate_manifest_file(self.proposed_service.skill_dir_for_record(skill) / "manifest.json")

    def _manifest_for_caller(self, caller: Skill) -> SkillManifest:
        return validate_manifest_file(self.proposed_service.skill_dir_for_record(caller) / "manifest.json")

    def _requirements_by_name(self, caller: Skill | None) -> set[str]:
        if caller is None:
            return set()
        try:
            manifest = self._manifest_for_caller(caller)
            return set(manifest.function_requirements)
        except (FileNotFoundError, ManifestValidationError, ProposedSkillError):
            return {
                str(requirement)
                for requirement in (caller.function_requirements_json or [])
                if isinstance(requirement, str) and requirement
            }

    def _access_state(self, caller: Skill | None, target: Skill) -> str:
        if caller is None:
            return "not_requested"
        if target.name not in self._requirements_by_name(caller):
            return "not_declared"
        contract = self.contract_for_skill(target)
        if contract.risk_level == "low":
            return "no_approval_required"
        approval = self._current_access_approval(caller, target)
        if approval is None:
            return "not_requested"
        current_fingerprint = self.target_contract_fingerprint(target)
        if approval.target_contract_fingerprint != current_fingerprint:
            return "stale"
        status = approval.approval_request.status
        return "stale" if status in {"expired", "superseded"} else status

    def _current_access_approval(
        self,
        caller: Skill,
        target: Skill,
    ) -> FunctionAccessApproval | None:
        approvals = self.db.scalars(
            select(FunctionAccessApproval)
            .where(FunctionAccessApproval.caller_skill_id == caller.id)
            .where(FunctionAccessApproval.target_skill_id == target.id)
            .where(FunctionAccessApproval.invalidated_at.is_(None))
            .order_by(FunctionAccessApproval.created_at.desc(), FunctionAccessApproval.id.desc())
        ).all()
        return approvals[0] if approvals else None

    def _create_access_request(
        self,
        caller: Skill,
        target: Skill,
        *,
        caller_version_id: int | None,
    ) -> FunctionAccessApproval:
        current = self._current_access_approval(caller, target)
        fingerprint = self.target_contract_fingerprint(target)
        if current is not None:
            if (
                current.target_contract_fingerprint == fingerprint
                and current.approval_request.status in {"pending", "approved"}
            ):
                return current
            self._invalidate_approval(current, "Superseded by current target function contract")
        contract = self.contract_for_skill(target)
        explanation = (
            f"Skill {caller.name} requests access to function {target.name}: {contract.description}. "
            f"The target's backend-derived risk is {contract.risk_level}. "
            "Approving allows only this caller-to-function relationship while the target risk and callable "
            "permission contract remain materially unchanged. It does not approve blocked permissions, enable "
            "either skill, or authorize other caller-to-function edges."
        )
        request = ApprovalRequest(
            skill_id=caller.id,
            request_scope="runtime",
            request_type="function_access",
            risk_level=contract.risk_level,
            requested_permissions_json=contract.permissions,
            requested_dependencies_json=self._active_manifest(target).dependencies,
            requested_network_domains_json=list(contract.permissions.get("network", [])),
            requested_filesystem_json={
                "filesystem_read": contract.permissions.get("filesystem_read", []),
                "filesystem_write": contract.permissions.get("filesystem_write", []),
            },
            reason_json={
                "caller_skill_id": caller.id,
                "caller_skill_name": caller.name,
                "caller_version_id": caller_version_id,
                "target_skill_id": target.id,
                "target_skill_name": target.name,
                "target_version_id": target.active_version_id,
                "target_description": contract.description,
                "target_risk_level": contract.risk_level,
                "target_contract_fingerprint": fingerprint,
                "approval_means": "This caller may invoke this target function through the backend control plane.",
                "approval_does_not_mean": [
                    "other skills may invoke the target",
                    "blocked or unsupported target permissions are allowed",
                    "the target is enabled or otherwise runnable",
                    "other edges in a nested function chain are authorized",
                ],
            },
            reason=explanation,
            user_explanation=explanation,
            status="pending",
        )
        self.db.add(request)
        self.db.flush()
        approval = FunctionAccessApproval(
            caller_skill_id=caller.id,
            target_skill_id=target.id,
            approval_request_id=request.id,
            target_contract_fingerprint=fingerprint,
        )
        self.db.add(approval)
        self.db.commit()
        self.db.refresh(approval)
        return approval

    def _invalidate_approval(self, approval: FunctionAccessApproval, reason: str) -> None:
        if approval.invalidated_at is not None:
            return
        approval.invalidated_at = utc_now()
        approval.invalidation_reason = reason
        if approval.approval_request.status in {"pending", "approved"}:
            approval.approval_request.status = "superseded"
        self.db.commit()

    def target_contract_fingerprint(self, target: Skill) -> str:
        manifest = self._active_manifest(target)
        graph = SkillGraphService(
            self.db,
            project_root=self.project_root,
        ).effective_contract(target, manifest=manifest)
        payload = {
            "risk_level": graph.risk_level,
            "permissions": graph.permissions,
            "dependencies": manifest.dependencies,
            "description": manifest.description,
            "input_schema": manifest.input_schema,
            "output_schema": manifest.output_schema,
            "requires_invocation_approval": (
                manifest.requires_invocation_approval or graph.risk_level == "high"
            ),
            "function_graph_fingerprint": graph.fingerprint,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _schema_error(value: dict[str, Any], schema: dict[str, Any] | None, label: str) -> str | None:
        if schema is None:
            return f"Function {label}_schema is missing"
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError as exc:
            path = ".".join(str(item) for item in exc.absolute_path)
            location = f" at {path}" if path else ""
            return f"Function {label} JSON is incompatible with the declared schema{location}: {exc.message}"
        return None

    @staticmethod
    def _merge_error(existing: str | None, current: str) -> str:
        if not existing:
            return current
        if current in existing:
            return existing
        return f"{existing}\n{current}"
