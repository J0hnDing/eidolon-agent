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

from app.models import (
    ApprovalRequest,
    FunctionAccessApproval,
    Skill,
    SkillRun,
    SkillVersion,
)
from app.schemas.function_registry import FunctionContractRead, FunctionRequirementReview
from app.schemas.manifest import SkillManifest, classify_permission_risk
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import FunctionRunContext, get_skill_runner, validate_supported_permissions


class FunctionRegistryError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class FunctionCaller:
    skill: Skill
    version_id: int
    run_id: int | None = None
    web_app_instance_id: str | None = None


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
        reasons = list(dict.fromkeys(reasons))
        availability = "available"
        if not skill.enabled and all(reason == "Function is disabled" for reason in reasons):
            availability = "disabled"
        elif reasons:
            availability = "disabled" if reasons == ["Function is disabled"] else "unavailable"
        permissions = manifest.permissions.model_dump(mode="json") if manifest is not None else {}
        dependencies = manifest.dependencies if manifest is not None else []
        risk_level = (
            classify_permission_risk(manifest.permissions, dependencies)
            if manifest is not None
            else skill.risk_level
        )
        return FunctionContractRead(
            skill_id=skill.id,
            name=skill.name,
            description=manifest.description if manifest is not None else skill.description,
            active_version_id=skill.active_version_id,
            active_version=active_version.version if active_version is not None else None,
            input_schema=manifest.input_schema if manifest is not None else skill.input_schema_json,
            output_schema=manifest.output_schema if manifest is not None else skill.output_schema_json,
            risk_level=risk_level,
            permissions=permissions,
            availability=availability,
            availability_reasons=reasons,
            declared_by_caller=declared_by_caller,
            access_state=access_state,
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

    def caller_from_capability(self, token: str) -> FunctionCaller:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        run = self.db.scalar(
            select(SkillRun)
            .where(SkillRun.function_capability_token_hash == token_hash)
            .where(SkillRun.status == "running")
            .where(SkillRun.ended_at.is_(None))
            .order_by(SkillRun.id.desc())
        )
        if run is None:
            raise FunctionRegistryError("Function caller capability is invalid or expired")
        caller = self.db.get(Skill, run.skill_id)
        if caller is None or run.version_id is None:
            raise FunctionRegistryError("Function caller identity is no longer available")
        valid_runtime = caller.runtime == "function" or (
            caller.runtime == "service"
            and run.invocation_source == "schedule"
            and run.source_schedule_id is not None
        )
        if (
            caller.status != "installed"
            or not valid_runtime
            or not caller.enabled
            or caller.active_version_id != run.version_id
        ):
            raise FunctionRegistryError("Runtime caller is no longer installed, eligible, and version-current")
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_run(caller)
        if not permission_decision.allowed:
            raise FunctionRegistryError(permission_decision.reason)
        return FunctionCaller(skill=caller, version_id=run.version_id, run_id=run.id)

    def invoke_from_capability(self, token: str, target_name: str, input_json: dict[str, Any]) -> SkillRun:
        caller = self.caller_from_capability(token)
        return self.invoke_declared(
            caller,
            target_name,
            input_json,
            source="skill",
            initiating_action=f"function_call_from_run_{caller.run_id}",
        )

    def invoke_from_web_app(
        self,
        caller_skill: Skill,
        caller_version_id: int,
        web_app_instance_id: str,
        target_name: str,
        input_json: dict[str, Any],
    ) -> SkillRun:
        return self.invoke_declared(
            FunctionCaller(
                skill=caller_skill,
                version_id=caller_version_id,
                web_app_instance_id=web_app_instance_id,
            ),
            target_name,
            input_json,
            source="web_app",
            initiating_action=f"web_app_instance_{web_app_instance_id}",
        )

    def invoke_declared(
        self,
        caller: FunctionCaller,
        target_name: str,
        input_json: dict[str, Any],
        *,
        source: str,
        initiating_action: str,
    ) -> SkillRun:
        requirement_declared = target_name in self._requirements_by_name(caller.skill)
        target = self.db.scalar(select(Skill).where(Skill.name == target_name))
        if target is None or target.runtime != "function":
            raise FunctionRegistryError("Target function is not installed")
        if not requirement_declared:
            return self._blocked_run(
                target,
                input_json,
                f"Caller skill {caller.skill.name} did not declare required function {target_name}",
                source=source,
                caller=caller,
                initiating_action=initiating_action,
            )
        contract = self.contract_for_skill(target)
        if contract.availability != "available":
            return self._blocked_run(
                target,
                input_json,
                "; ".join(contract.availability_reasons) or "Target function is unavailable",
                source=source,
                caller=caller,
                initiating_action=initiating_action,
            )
        if contract.risk_level in {"medium", "high"} and self._access_state(caller.skill, target) != "approved":
            return self._blocked_run(
                target,
                input_json,
                f"Caller-specific approval is required for {caller.skill.name} to invoke {target.name}",
                source=source,
                caller=caller,
                initiating_action=initiating_action,
            )
        return self._invoke(
            target,
            input_json,
            source=source,
            caller=caller,
            initiating_action=initiating_action,
        )

    def invoke_direct(
        self,
        target: Skill,
        input_json: dict[str, Any],
        *,
        source: str = "direct_user",
        source_schedule_id: int | None = None,
        initiating_action: str | None = None,
    ) -> SkillRun:
        return self._invoke(
            target,
            input_json,
            source=source,
            source_schedule_id=source_schedule_id,
            initiating_action=initiating_action or source,
        )

    def _invoke(
        self,
        target: Skill,
        input_json: dict[str, Any],
        *,
        source: str,
        caller: FunctionCaller | None = None,
        source_schedule_id: int | None = None,
        initiating_action: str | None = None,
    ) -> SkillRun:
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
            return self._blocked_run(
                target,
                input_json,
                "; ".join(availability_reasons),
                source=source,
                caller=caller,
                source_schedule_id=source_schedule_id,
                initiating_action=initiating_action,
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
            return self._blocked_run(
                target,
                input_json,
                input_error,
                source=source,
                caller=caller,
                source_schedule_id=source_schedule_id,
                initiating_action=initiating_action,
            )
        capability_token = secrets.token_urlsafe(32)
        context = FunctionRunContext(
            version_id=target.active_version_id,
            invocation_source=source,
            caller_skill_id=caller.skill.id if caller is not None else None,
            caller_version_id=caller.version_id if caller is not None else None,
            source_schedule_id=source_schedule_id,
            web_app_instance_id=caller.web_app_instance_id if caller is not None else None,
            initiating_action=initiating_action,
            capability_token=capability_token,
        )
        try:
            with SkillOperationGuard(self.db).locked(target, "run", reason=initiating_action or source):
                runner = self.runner_factory(self.db)
                try:
                    run = runner.run(
                        skill_id=target.id,
                        skill_dir=self.proposed_service.skill_dir_for_record(target),
                        input_json=input_json,
                        context=context,
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
            return self._blocked_run(
                target,
                input_json,
                str(exc),
                source=source,
                caller=caller,
                source_schedule_id=source_schedule_id,
                initiating_action=initiating_action,
            )
        if run.output_json is not None and contract.output_schema is not None:
            output_error = self._schema_error(run.output_json, contract.output_schema, "output")
            if output_error:
                run.status = "failed"
                run.error_message = self._merge_error(run.error_message, output_error)
                run.ended_at = run.ended_at or utc_now()
                self.db.commit()
                self.db.refresh(run)
        return run

    def _blocked_run(
        self,
        target: Skill,
        input_json: dict[str, Any],
        reason: str,
        *,
        source: str,
        caller: FunctionCaller | None = None,
        source_schedule_id: int | None = None,
        initiating_action: str | None = None,
    ) -> SkillRun:
        run = SkillRun(
            skill_id=target.id,
            version_id=target.active_version_id,
            status="blocked",
            input_json=input_json,
            started_at=utc_now(),
            ended_at=utc_now(),
            error_message=reason,
            invocation_source=source,
            caller_skill_id=caller.skill.id if caller is not None else None,
            caller_version_id=caller.version_id if caller is not None else None,
            source_schedule_id=source_schedule_id,
            web_app_instance_id=caller.web_app_instance_id if caller is not None else None,
            initiating_action=initiating_action,
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
        payload = {
            "risk_level": classify_permission_risk(manifest.permissions, manifest.dependencies),
            "permissions": manifest.permissions.model_dump(mode="json"),
            "dependencies": manifest.dependencies,
            "input_schema": manifest.input_schema,
            "output_schema": manifest.output_schema,
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
