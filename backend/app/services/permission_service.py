from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentRunStep, ApprovalRequest, Skill, SkillGenerationRequest
from app.services.manifest_validator import validate_manifest_file
from app.services.proposed_skill_service import ProposedSkillService


class PermissionError(ValueError):
    pass


@dataclass
class PermissionDecision:
    allowed: bool
    reason: str


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PermissionService:
    db: Session
    project_root: Path | None = None

    def __post_init__(self) -> None:
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)

    def create_build_time_request(self, generation_request: SkillGenerationRequest) -> ApprovalRequest:
        existing = self._latest_request(generation_request_id=generation_request.id, scope="build_time")
        if existing and existing.status == "pending":
            return self._refresh_build_time_request(existing, generation_request)
        if existing and existing.status in {"approved", "denied"}:
            return existing

        return self._create_new_build_time_request(generation_request)

    def _create_new_build_time_request(self, generation_request: SkillGenerationRequest) -> ApprovalRequest:
        plan = generation_request.plan_json
        permission_plan = self._permission_plan_from_plan(plan)
        runtime_plan = permission_plan["runtime"]
        build_time_plan = permission_plan["build_time"]
        future_permissions = self._runtime_permissions(runtime_plan)
        dependencies = list(runtime_plan["dependencies"])
        network = list(future_permissions["network"])
        permissions = {
            "codex_generation": bool(build_time_plan.get("codex_generation", True)),
            "internet_research": bool(build_time_plan.get("internet_research", bool(network or dependencies))),
            "future_runtime_permissions": future_permissions,
            "future_runtime_network": network,
            "future_runtime_filesystem_write": future_permissions.get("filesystem_write", []),
        }
        filesystem = {
            "filesystem_read": future_permissions.get("filesystem_read", []),
            "filesystem_write": future_permissions.get("filesystem_write", []),
        }
        risk_level, blocked_reasons = self._risk_for_permissions(
            future_permissions,
            dependencies=dependencies,
        )
        explanation = self._build_time_explanation(plan, dependencies, network, blocked_reasons)
        request = ApprovalRequest(
            generation_request_id=generation_request.id,
            request_scope="build_time",
            request_type="generation",
            risk_level=risk_level,
            requested_permissions_json=permissions,
            requested_dependencies_json=dependencies,
            requested_network_domains_json=network,
            requested_filesystem_json=filesystem,
            reason_json={
                "blocked_reasons": blocked_reasons,
                "approval_means": "Codex may generate proposed skill files only.",
                "approval_does_not_mean": [
                    "installing the skill",
                    "running the skill",
                    "installing packages",
                    "approving runtime permissions",
                ],
            },
            reason=explanation,
            user_explanation=explanation,
            status="pending",
        )
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)
        return request

    def _refresh_build_time_request(
        self,
        request: ApprovalRequest,
        generation_request: SkillGenerationRequest,
    ) -> ApprovalRequest:
        plan = generation_request.plan_json
        permission_plan = self._permission_plan_from_plan(plan)
        runtime_plan = permission_plan["runtime"]
        build_time_plan = permission_plan["build_time"]
        future_permissions = self._runtime_permissions(runtime_plan)
        dependencies = list(runtime_plan["dependencies"])
        network = list(future_permissions["network"])
        permissions = {
            "codex_generation": bool(build_time_plan.get("codex_generation", True)),
            "internet_research": bool(build_time_plan.get("internet_research", bool(network or dependencies))),
            "future_runtime_permissions": future_permissions,
            "future_runtime_network": network,
            "future_runtime_filesystem_write": future_permissions.get("filesystem_write", []),
        }
        filesystem = {
            "filesystem_read": future_permissions.get("filesystem_read", []),
            "filesystem_write": future_permissions.get("filesystem_write", []),
        }
        risk_level, blocked_reasons = self._risk_for_permissions(future_permissions, dependencies=dependencies)
        explanation = self._build_time_explanation(plan, dependencies, network, blocked_reasons)
        previous_reason = request.reason_json or {}
        request.risk_level = risk_level
        request.requested_permissions_json = permissions
        request.requested_dependencies_json = dependencies
        request.requested_network_domains_json = network
        request.requested_filesystem_json = filesystem
        request.reason_json = {
            **previous_reason,
            "blocked_reasons": blocked_reasons,
            "approval_means": "Codex may generate proposed skill files only.",
            "approval_does_not_mean": [
                "installing the skill",
                "running the skill",
                "installing packages",
                "approving runtime permissions",
            ],
        }
        if "permission_review_summary" in request.reason_json:
            request.reason_json["permission_review_summary"] = explanation
        request.reason = explanation
        if "product_manager_summary" in request.reason_json:
            request.user_explanation = (
                f"ProductManager: {request.reason_json['product_manager_summary']}\n\n"
                f"Permission review: {request.reason_json.get('permission_review_summary', explanation)}"
            )
            request.reason = request.user_explanation
        else:
            request.user_explanation = explanation
        self.db.commit()
        self.db.refresh(request)
        return request

    def create_update_build_time_request(
        self,
        skill: Skill,
        agent_run: AgentRun,
        blueprint: dict[str, Any],
        product_manager_summary: str,
    ) -> ApprovalRequest:
        existing_requests = self.db.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.skill_id == skill.id)
            .where(ApprovalRequest.request_scope == "build_time")
            .where(ApprovalRequest.request_type == "update")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        ).all()
        for existing in existing_requests:
            if (existing.reason_json or {}).get("agent_run_id") == agent_run.id and existing.status in {
                "pending",
                "approved",
                "denied",
            }:
                return existing

        permission_plan = self._permission_plan_from_plan(blueprint)
        future_permissions = self._runtime_permissions(permission_plan["runtime"])
        dependencies = list(permission_plan["runtime"]["dependencies"])
        network = list(future_permissions["network"])
        filesystem = {
            "filesystem_read": future_permissions.get("filesystem_read", []),
            "filesystem_write": future_permissions.get("filesystem_write", []),
        }
        risk_level, blocked_reasons = self._risk_for_permissions(future_permissions, dependencies=dependencies)
        permissions = {
            "codex_generation": True,
            "internet_research": bool(network or dependencies),
            "future_runtime_permissions": future_permissions,
            "future_runtime_network": network,
            "future_runtime_filesystem_write": future_permissions.get("filesystem_write", []),
        }
        explanation = (
            f"ProductManager wants to update {skill.name}. "
            "Approving this lets Builder create a draft version only; it does not activate, install, or run the skill."
        )
        request = ApprovalRequest(
            skill_id=skill.id,
            request_scope="build_time",
            request_type="update",
            risk_level=risk_level,
            requested_permissions_json=permissions,
            requested_dependencies_json=dependencies,
            requested_network_domains_json=network,
            requested_filesystem_json=filesystem,
            reason_json={
                "agent_run_id": agent_run.id,
                "blueprint_json": blueprint,
                "blocked_reasons": blocked_reasons,
                "product_manager_summary": product_manager_summary,
                "approval_means": "Builder may create a draft version folder for this update.",
                "approval_does_not_mean": [
                    "activating the new version",
                    "running the skill",
                    "installing packages silently",
                    "approving runtime permissions",
                ],
            },
            reason=explanation,
            user_explanation=explanation,
            status="pending",
        )
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)
        return request

    def create_runtime_request(self, skill: Skill) -> ApprovalRequest:
        existing = self._latest_request(skill_id=skill.id, scope="runtime", request_type="install")
        if existing and existing.status in {"pending", "approved", "denied"}:
            return existing

        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        manifest = validate_manifest_file(skill_dir / "manifest.json")
        permissions = manifest.permissions.model_dump()
        dependencies = list(manifest.dependencies)
        risk_level, blocked_reasons = self._risk_for_permissions(permissions, dependencies=dependencies)
        expansion = self.detect_permission_expansion(skill, permissions)
        dependency_expansion = self.detect_dependency_expansion(skill, dependencies)
        if expansion:
            risk_level = "blocked" if risk_level == "blocked" else "medium"
        if dependency_expansion:
            risk_level = "blocked" if risk_level == "blocked" else "medium"
        explanation = self._runtime_explanation(skill, permissions, blocked_reasons, expansion, dependencies)
        request = ApprovalRequest(
            skill_id=skill.id,
            request_scope="runtime",
            request_type="install",
            risk_level=risk_level,
            requested_permissions_json=permissions,
            requested_dependencies_json=dependencies,
            requested_network_domains_json=list(permissions.get("network", [])),
            requested_filesystem_json={
                "filesystem_read": permissions.get("filesystem_read", []),
                "filesystem_write": permissions.get("filesystem_write", []),
            },
            reason_json={
                "blocked_reasons": blocked_reasons,
                "permission_expansion": expansion,
                "dependency_expansion": dependency_expansion,
                "runner_unsupported": self.unsupported_runtime_reasons(permissions),
                "runner_network_enforcement": (
                    "Approved network domains enable container network access for this MVP; "
                    "domain-level egress filtering is not enforced yet."
                )
                if permissions.get("network")
                else "",
            },
            reason=explanation,
            user_explanation=explanation,
            status="pending",
        )
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)
        return request

    def detect_permission_expansion(self, skill: Skill, actual_permissions: dict[str, Any]) -> dict[str, Any]:
        generation_request = self.db.scalar(
            select(SkillGenerationRequest)
            .where(SkillGenerationRequest.proposed_skill_id == skill.id)
            .order_by(SkillGenerationRequest.created_at.desc())
        )
        if generation_request is None:
            return {}
        planned = generation_request.plan_json.get("requested_permissions", {})
        expansion: dict[str, Any] = {}
        for key in ("network", "filesystem_read", "filesystem_write", "secrets"):
            added = sorted(set(actual_permissions.get(key, [])) - set(planned.get(key, [])))
            if added:
                expansion[key] = added
        if actual_permissions.get("shell") and not planned.get("shell"):
            expansion["shell"] = True
        actual_codex = actual_permissions.get("codex")
        planned_codex = planned.get("codex") if isinstance(planned, dict) else None
        actual_codex_internet = bool(actual_codex.get("internet_access")) if isinstance(actual_codex, dict) else False
        planned_codex_internet = (
            bool(planned_codex.get("internet_access"))
            if isinstance(planned_codex, dict)
            else bool(planned.get("network"))
        )
        if actual_codex_internet and not planned_codex_internet:
            expansion["codex"] = {"internet_access": True}
        return expansion

    def detect_dependency_expansion(self, skill: Skill, actual_dependencies: list[str]) -> list[str]:
        generation_request = self.db.scalar(
            select(SkillGenerationRequest)
            .where(SkillGenerationRequest.proposed_skill_id == skill.id)
            .order_by(SkillGenerationRequest.created_at.desc())
        )
        if generation_request is None:
            return []
        planned = set(generation_request.plan_json.get("requested_dependencies", []))
        return sorted(set(actual_dependencies) - planned)

    def approve_request(self, request: ApprovalRequest, notes: str | None = None) -> ApprovalRequest:
        if request.status == "denied":
            raise PermissionError("Denied permission requests cannot be approved")
        if request.status == "approved":
            self._sync_agent_permission_steps(request, approved=True)
            return request
        if request.status == "pending" and request.request_scope == "build_time" and request.generation_request is not None:
            request = self._refresh_build_time_request(request, request.generation_request)
        if request.risk_level == "blocked":
            raise PermissionError("Blocked or unsupported permission requests cannot be approved in this milestone")
        request.status = "approved"
        request.resolved_at = utc_now()
        request.resolved_by = "local_user"
        request.decision_notes = notes
        if request.request_scope == "build_time" and request.generation_request is not None:
            request.generation_request.status = "approved"
        self.db.commit()
        self.db.refresh(request)
        self._sync_agent_permission_steps(request, approved=True)
        return request

    def deny_request(self, request: ApprovalRequest, notes: str | None = None) -> ApprovalRequest:
        if request.status == "approved":
            raise PermissionError("Approved permission requests cannot be denied later in this milestone")
        if request.status == "denied":
            self._sync_agent_permission_steps(request, approved=False)
            return request
        request.status = "denied"
        request.resolved_at = utc_now()
        request.resolved_by = "local_user"
        request.decision_notes = notes
        if request.request_scope == "build_time" and request.generation_request is not None:
            request.generation_request.status = "cancelled"
        self.db.commit()
        self.db.refresh(request)
        self._sync_agent_permission_steps(request, approved=False)
        return request

    def can_generate(self, generation_request: SkillGenerationRequest) -> PermissionDecision:
        request = self._latest_request(generation_request_id=generation_request.id, scope="build_time")
        if request is None:
            return PermissionDecision(False, "Build-time permissions have not been reviewed")
        if request.status != "approved":
            return PermissionDecision(False, f"Build-time permission request is {request.status}")
        return PermissionDecision(True, "Build-time permissions are approved")

    def can_install(self, skill: Skill) -> PermissionDecision:
        request = self._latest_active_runtime_request(skill)
        if request is None:
            return PermissionDecision(False, "Runtime permissions have not been reviewed")
        if request.risk_level == "blocked":
            return PermissionDecision(False, "Runtime permissions include blocked or unsupported requests")
        if request.status != "approved":
            return PermissionDecision(False, f"Runtime permission request is {request.status}")
        return PermissionDecision(True, "Runtime permissions are approved")

    def can_run(self, skill: Skill) -> PermissionDecision:
        install_decision = self.can_install(skill)
        if not install_decision.allowed:
            return install_decision
        request = self._latest_active_runtime_request(skill)
        unsupported = self.unsupported_runtime_reasons(request.requested_permissions_json if request else {})
        if unsupported:
            return PermissionDecision(False, "; ".join(unsupported))
        return PermissionDecision(True, "Runtime permissions are approved and supported")

    def _latest_active_runtime_request(self, skill: Skill) -> ApprovalRequest | None:
        requests = self.db.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.skill_id == skill.id)
            .where(ApprovalRequest.request_scope == "runtime")
            .where(ApprovalRequest.request_type == "install")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        ).all()
        for request in requests:
            version_id = (request.reason_json or {}).get("version_id")
            if version_id is None or version_id == skill.active_version_id:
                return request
        return None

    def unsupported_runtime_reasons(self, permissions: dict[str, Any]) -> list[str]:
        reasons = []
        if permissions.get("filesystem_read"):
            reasons.append("Filesystem read permissions are not supported until a safe file picker exists.")
        if permissions.get("secrets"):
            reasons.append("Secrets access is not supported in this milestone.")
        if permissions.get("shell"):
            reasons.append("Shell access is not supported in this milestone.")
        writes = [path for path in permissions.get("filesystem_write", []) if self._normalize_path(path) != "./cache"]
        if writes:
            reasons.append("Filesystem writes outside ./cache are not supported.")
        codex_permissions = permissions.get("codex", {"call_response": True, "internet_access": bool(permissions.get("network"))})
        if not isinstance(codex_permissions, dict):
            reasons.append("Codex permissions must be an object.")
        else:
            unsupported_codex_keys = sorted(set(codex_permissions) - {"call_response", "internet_access"})
            if unsupported_codex_keys:
                reasons.append("Codex permissions other than call_response and internet_access are not supported.")
            if codex_permissions.get("call_response") is False:
                reasons.append("Codex call/response permission is required.")
            if codex_permissions.get("internet_access") and not permissions.get("network"):
                reasons.append("Codex internet access requires runtime network permission.")
        return reasons

    def _normalize_codex_permissions(
        self,
        raw_permissions: dict[str, Any],
        network: list[str],
    ) -> dict[str, bool]:
        raw_codex = raw_permissions.get("codex")
        if not isinstance(raw_codex, dict):
            raw_codex = {}
        normalized = {
            "call_response": bool(raw_codex.get("call_response", True)),
            "internet_access": bool(raw_codex.get("internet_access", bool(network))),
        }
        for key, value in raw_codex.items():
            if key not in normalized:
                normalized[str(key)] = bool(value)
        return normalized

    def _latest_request(
        self,
        *,
        skill_id: int | None = None,
        generation_request_id: int | None = None,
        scope: str,
        request_type: str | None = None,
    ) -> ApprovalRequest | None:
        query = select(ApprovalRequest).where(ApprovalRequest.request_scope == scope)
        if request_type is not None:
            query = query.where(ApprovalRequest.request_type == request_type)
        if skill_id is not None:
            query = query.where(ApprovalRequest.skill_id == skill_id)
        if generation_request_id is not None:
            query = query.where(ApprovalRequest.generation_request_id == generation_request_id)
        return self.db.scalar(query.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc()))

    def _permission_plan_from_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        value = plan.get("permission_plan")
        if not isinstance(value, dict):
            value = {}
        runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
        build_time = value.get("build_time") if isinstance(value.get("build_time"), dict) else {}
        fallback_permissions = plan.get(
            "requested_permissions",
            {"network": [], "filesystem_read": [], "filesystem_write": [], "secrets": [], "shell": False},
        )
        if not isinstance(fallback_permissions, dict):
            fallback_permissions = {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": [],
                "secrets": [],
                "shell": False,
            }
        raw_permissions = runtime.get("permissions") if isinstance(runtime.get("permissions"), dict) else {**fallback_permissions, **runtime}
        permissions = {
            "network": list(raw_permissions.get("network", []) or []),
            "filesystem_read": list(raw_permissions.get("filesystem_read", []) or []),
            "filesystem_write": list(raw_permissions.get("filesystem_write", []) or []),
            "secrets": list(raw_permissions.get("secrets", []) or []),
            "shell": bool(raw_permissions.get("shell", False)),
        }
        dependencies = list(runtime.get("dependencies", plan.get("requested_dependencies", [])) or [])
        network = list(permissions["network"] or plan.get("requested_network_domains", []) or [])
        permissions["codex"] = self._normalize_codex_permissions(raw_permissions, network)
        return {
            "build_time": {
                "codex_generation": bool(build_time.get("codex_generation", True)),
                "internet_research": bool(build_time.get("internet_research", bool(network or dependencies))),
                "dependencies": list(build_time.get("dependencies", dependencies) or []),
                "reason": str(build_time.get("reason") or "Codex needs to generate controlled skill files."),
            },
            "runtime": {
                **permissions,
                "dependencies": dependencies,
                "reason": str(runtime.get("reason") or "Expected runtime permissions for this skill."),
            },
        }

    def _runtime_permissions(self, runtime_plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "network": list(runtime_plan.get("network", []) or []),
            "filesystem_read": list(runtime_plan.get("filesystem_read", []) or []),
            "filesystem_write": list(runtime_plan.get("filesystem_write", []) or []),
            "secrets": list(runtime_plan.get("secrets", []) or []),
            "shell": bool(runtime_plan.get("shell", False)),
            "codex": runtime_plan.get(
                "codex",
                {"call_response": True, "internet_access": bool(runtime_plan.get("network"))},
            ),
        }

    def _risk_for_permissions(
        self,
        permissions: dict[str, Any],
        *,
        dependencies: list[str],
    ) -> tuple[str, list[str]]:
        blocked = []
        network = permissions.get("network", [])
        if any(domain in {"*", "all", "0.0.0.0/0"} or "*" in str(domain) for domain in network):
            blocked.append("Unrestricted or wildcard network access is blocked.")
        unsupported_reads = [
            path for path in permissions.get("filesystem_read", []) if self._normalize_path(path) != "./cache"
        ]
        if unsupported_reads:
            blocked.append(
                "Filesystem read access is blocked until user-selected folders are implemented: "
                + ", ".join(str(path) for path in unsupported_reads)
            )
        for path in permissions.get("filesystem_write", []):
            normalized = self._normalize_path(path)
            if normalized != "./cache":
                blocked.append(f"Filesystem write access outside ./cache is blocked: {path}")
            if Path(str(path)).is_absolute() or ".." in Path(str(path)).parts:
                blocked.append(f"Unsafe filesystem path is blocked: {path}")
        if permissions.get("secrets"):
            blocked.append("Secrets access is blocked in this milestone.")
        if permissions.get("shell"):
            blocked.append("Shell access is blocked in this milestone.")
        codex_permissions = permissions.get("codex", {"call_response": True, "internet_access": bool(network)})
        if not isinstance(codex_permissions, dict):
            blocked.append("Codex permissions must be an object.")
            codex_permissions = {}
        unsupported_codex_keys = sorted(set(codex_permissions) - {"call_response", "internet_access"})
        if unsupported_codex_keys:
            blocked.append(
                "Codex permissions other than call_response and internet_access are blocked: "
                + ", ".join(unsupported_codex_keys)
            )
        if codex_permissions.get("call_response") is False:
            blocked.append("Codex call/response cannot be disabled for generated skills in this milestone.")
        if codex_permissions.get("internet_access") and not network:
            blocked.append("Codex internet access requires approved runtime network domains.")
        if blocked:
            return "blocked", blocked
        if network or dependencies or codex_permissions.get("internet_access"):
            return "medium", []
        if permissions.get("filesystem_write"):
            return "low", []
        return "low", []

    def _normalize_path(self, path: str) -> str:
        value = str(path).replace("\\", "/").rstrip("/")
        if not value.startswith("./"):
            value = f"./{value}"
        return value

    def _build_time_explanation(
        self,
        plan: dict[str, Any],
        dependencies: list[str],
        network: list[str],
        blocked_reasons: list[str],
    ) -> str:
        parts = [
            f"Codex wants to generate a proposed {plan.get('skill_type')} skill named {plan.get('skill_name')}.",
            "Approving this only allows proposed skill generation; it does not install, run, or install packages.",
        ]
        if network:
            parts.append(
                "The proposed design may later request network access to "
                + ", ".join(network)
                + ". Runtime execution still requires separate manifest-based approval; domain-level filtering is not enforced yet."
            )
        if dependencies:
            parts.append(
                "The proposed design mentions package dependencies "
                + ", ".join(dependencies)
                + ". The app will not install them automatically."
            )
        if blocked_reasons:
            parts.append("Blocked requests: " + " ".join(blocked_reasons))
        return " ".join(parts)

    def _runtime_explanation(
        self,
        skill: Skill,
        permissions: dict[str, Any],
        blocked_reasons: list[str],
        expansion: dict[str, Any],
        dependencies: list[str],
    ) -> str:
        parts = [f"The generated skill {skill.name} declares these runtime permissions from manifest.json."]
        if permissions.get("network"):
            parts.append(
                "It requests network access to "
                + ", ".join(permissions["network"])
                + ". Approving runtime permissions allows container network access for this MVP; domain-level filtering is not enforced yet."
            )
        if dependencies:
            parts.append(
                "It declares Python package dependencies "
                + ", ".join(dependencies)
                + ". These must have been approved and installed into the skill-local dependency folder before runtime."
            )
        if permissions.get("filesystem_write"):
            parts.append("It requests filesystem write access to " + ", ".join(permissions["filesystem_write"]) + ".")
        if expansion:
            parts.append(f"Permission expansion detected: {expansion}.")
        if blocked_reasons:
            parts.append("Blocked requests: " + " ".join(blocked_reasons))
        return " ".join(parts)

    def _sync_agent_permission_steps(self, request: ApprovalRequest, *, approved: bool) -> None:
        steps = self.db.scalars(
            select(AgentRunStep)
            .where(AgentRunStep.status == "waiting_for_approval")
        ).all()
        changed_run_ids: set[int] = set()
        for step in steps:
            if not self._step_matches_permission_request(step, request.id):
                continue
            step.status = "succeeded" if approved else "failed"
            step.ended_at = utc_now()
            output = dict(step.output_json or {})
            if "permission_review_json" in output and isinstance(output["permission_review_json"], dict):
                output["permission_review_json"] = {
                    **output["permission_review_json"],
                    "status": request.status,
                }
            elif "security_review_json" in output and isinstance(output["security_review_json"], dict):
                output["security_review_json"] = {**output["security_review_json"], "status": request.status}
            else:
                output["status"] = request.status
            step.output_json = output
            decision = "Approved" if approved else "Denied"
            step.logs = f"{step.logs or ''}\n\n{decision} by local user.".strip()
            changed_run_ids.add(step.agent_run_id)

        for run_id in changed_run_ids:
            agent_run = self.db.get(AgentRun, run_id)
            if agent_run is None:
                continue
            if request.request_scope == "build_time":
                agent_run.status = "pending" if approved else "cancelled"
                agent_run.current_step = "product_manager"
                if approved:
                    agent_run.summary = "Build-time approval is approved. Resume the agent run to continue generation."
                if not approved:
                    agent_run.completed_at = utc_now()
                    agent_run.error_message = "Build-time approval was denied."
            elif request.request_scope == "runtime" and agent_run.status == "waiting_for_approval":
                agent_run.status = "succeeded" if approved else "blocked"
                if not approved:
                    agent_run.error_message = "Runtime permission approval was denied."
        if changed_run_ids:
            self.db.commit()

    def _step_matches_permission_request(self, step: AgentRunStep, request_id: int) -> bool:
        output = step.output_json or {}
        if output.get("permission_request_id") == request_id:
            return True
        permission_review = output.get("permission_review_json")
        if isinstance(permission_review, dict) and permission_review.get("permission_request_id") == request_id:
            return True
        security_review = output.get("security_review_json")
        return isinstance(security_review, dict) and security_review.get("permission_request_id") == request_id
