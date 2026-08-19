import json
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentRunStep, ApprovalRequest, Skill, SkillGenerationRequest
from app.schemas.manifest import ManifestPermissions, classify_permission_risk
from app.schemas.proposed_skill import ProposedSkillValidationRead
from app.services.agent_run_artifact_store import AgentRunArtifactStore
from app.services.build_dependency_service import BuildDependencyError, BuildDependencyService
from app.services.capability_scanner import CapabilityScanResult, StaticCapabilityScanner
from app.services.codex_routing_service import CodexRoutingService
from app.services.codex_service import CodexGenerationError, CodexService
from app.services.codex_usage_service import codex_usage_service
from app.services.default_permissions import (
    agent_permission_bounds,
    default_build_time_dependencies,
    effective_permission_plan,
    planning_permission_policy,
)
from app.services.function_catalog_service import FunctionCatalogService
from app.services.integration_registry import OPERATIONS
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_operation_guard import SkillOperationGuard
from app.services.skill_package_files import snapshot_skill_files
from app.services.skill_version_service import SkillVersionError, SkillVersionService
from app.services.task_dag_service import TaskDagService
from app.workflows.base import (
    DEFAULT_BUILD_WORKFLOW,
    MAX_TASK_FAILURES,
    ProjectBuildWorkflowError,
)
from app.workflows.registry import get_project_build_workflow

AgentWorkflowError = ProjectBuildWorkflowError


DEFAULT_TASK_ID = "core_skill"
CODEX_USAGE_RESERVE_PERCENT = 5
_GENERATION_PLANNING_LOCKS: dict[int, threading.Lock] = {}
_GENERATION_PLANNING_LOCKS_GUARD = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class AgentWorkflowService:
    db: Session
    codex_service: CodexService | None = None
    project_root: Path | None = None

    def __post_init__(self) -> None:
        if self.project_root is None:
            self.project_root = Path(__file__).resolve().parents[3]
        self.project_root = self.project_root.resolve()
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)
        self.task_dags = TaskDagService()
        self.artifacts = AgentRunArtifactStore(self.db, self.project_root, self.task_dags)
        if self.codex_service is None:
            self.codex_service = CodexService(self.db, project_root=self.project_root)

    def create_build_run(self, generation_request: SkillGenerationRequest) -> AgentRun:
        with _GENERATION_PLANNING_LOCKS_GUARD:
            planning_lock = _GENERATION_PLANNING_LOCKS.setdefault(generation_request.id, threading.Lock())
        if not planning_lock.acquire(blocking=False):
            raise AgentWorkflowError("ProductManager is already processing this generation request")
        try:
            return self._create_build_run_locked(generation_request)
        finally:
            planning_lock.release()

    def _create_build_run_locked(self, generation_request: SkillGenerationRequest) -> AgentRun:
        existing = self.latest_run_for_generation(generation_request.id)
        if existing and existing.status in {"pending", "running", "waiting_for_approval", "paused", "succeeded"}:
            return existing
        if (
            existing
            and existing.status == "blocked"
            and (existing.final_summary_json or {}).get("decision_json", {}).get("decision") == "ask_user_for_input"
        ):
            return self._plan_build(generation_request, existing, initial=False)

        agent_run = AgentRun(
            run_type="build_skill",
            status="running",
            generation_request_id=generation_request.id,
            user_request=generation_request.user_message,
            current_step="product_manager",
            failure_count_json={},
            summary="ProductManager is reviewing whether the project is clear and plausible.",
        )
        self.db.add(agent_run)
        self.db.commit()
        self.db.refresh(agent_run)
        self.artifacts.initialize(agent_run)
        return self._plan_build(generation_request, agent_run, initial=True)

    def _plan_build(
        self,
        generation_request: SkillGenerationRequest,
        agent_run: AgentRun,
        *,
        initial: bool,
    ) -> AgentRun:
        agent_run.status = "running"
        agent_run.completed_at = None
        agent_run.error_message = None
        agent_run.user_request = generation_request.user_message
        self.db.commit()

        if initial:
            intent_prompt = {
                "schema_version": 1,
                "refined_prompt": generation_request.user_message,
            }
            intent_path = self.artifacts.write_json(agent_run, "intent_prompt.json", intent_prompt)
            intent_step = self._start_step(
                agent_run,
                "product_manager",
                input_json={
                    "action": "pm_refine_intent",
                    "user_request": generation_request.user_message,
                },
                logs="Intent refinement placeholder copied the initial Project-mode request.",
            )
            self._finish_step(
                agent_run,
                intent_step,
                "succeeded",
                output_json={"intent_prompt": intent_prompt, "intent_prompt_path": intent_path},
                logs="Intent refinement placeholder wrote intent_prompt.json without invoking Codex.",
            )
        else:
            intent_prompt = self.artifacts.read_json(agent_run, "intent_prompt.json")
            if not intent_prompt:
                raise AgentWorkflowError("The stored ProductManager intent prompt is missing")

        downstream_intent = self._downstream_intent_prompt(intent_prompt)
        step = self._start_step(
            agent_run,
            "product_manager",
            input_json={
                "action": "pm_plan_build",
                "intent_prompt": downstream_intent if initial else None,
                "user_reply": None if initial else self.codex_service._latest_project_user_reply(generation_request),
                "permission_policy": planning_permission_policy() if initial else None,
            },
            logs="ProductManager is clarifying, rejecting, or creating the complete build plan.",
        )
        try:
            review = self.codex_service.product_manager_plan_build(generation_request, downstream_intent)
        except CodexGenerationError as exc:
            self._finish_step(agent_run, step, "failed", error_message=str(exc), logs=str(exc))
            self._fail_run(agent_run, str(exc))
            generation_request.status = "failed"
            generation_request.error_message = str(exc)
            self.db.commit()
            return agent_run
        decision = str(review["decision"])
        user_prompt = review.get("user_prompt")
        summary = (
            str(user_prompt)
            if user_prompt
            else "ProductManager created a complete build plan for approval."
        )
        decision_json = {"decision": decision, "user_prompt": user_prompt}
        decision_path = self.artifacts.write_json(agent_run, "decision.json", decision_json)

        if decision == "ask_user_for_input":
            prompt = str(user_prompt)
            self._record_pending_product_manager_question(generation_request, prompt)
            self._finish_step(
                agent_run,
                step,
                "blocked",
                output_json={
                    "decision_json": {"decision": "ask_user_for_input"},
                    "decision_path": decision_path,
                    "user_summary": summary,
                    "user_prompt": prompt,
                },
                logs=summary,
            )
            generation_request.status = "needs_input"
            generation_request.error_message = prompt
            agent_run.status = "blocked"
            agent_run.current_step = "product_manager"
            agent_run.current_task_id = None
            agent_run.summary = prompt
            agent_run.final_summary_json = {
                "decision_json": {"decision": "ask_user_for_input"},
                "decision_path": decision_path,
                "user_summary": summary,
                "user_prompt": prompt,
            }
            self.db.commit()
            self.db.refresh(agent_run)
            return agent_run

        if decision == "stop_inplausible":
            archive_warning = self.codex_service.archive_product_manager_thread(generation_request)
            self._finish_step(
                agent_run,
                step,
                "blocked",
                output_json={
                    "decision_json": {"decision": "stop_inplausible"},
                    "decision_path": decision_path,
                    "user_summary": summary,
                    "thread_archive_warning": archive_warning,
                },
                logs=summary,
            )
            generation_request.status = "failed"
            generation_request.error_message = summary
            agent_run.status = "blocked"
            agent_run.current_step = "product_manager"
            agent_run.current_task_id = None
            agent_run.summary = summary
            agent_run.final_summary_json = {
                "decision_json": {"decision": "stop_inplausible"},
                "decision_path": decision_path,
                "user_summary": summary,
                "thread_archive_warning": archive_warning,
            }
            agent_run.completed_at = utc_now()
            self.db.commit()
            self.db.refresh(agent_run)
            return agent_run

        return self._create_build_artifacts_after_plan(
            generation_request,
            agent_run,
            step,
            downstream_intent,
            review,
            decision_path,
        )

    def _create_build_artifacts_after_plan(
        self,
        generation_request: SkillGenerationRequest,
        agent_run: AgentRun,
        step: AgentRunStep,
        intent_prompt: dict[str, Any],
        planned: dict[str, object],
        decision_path: str,
    ) -> AgentRun:
        blueprint = planned.get("blueprint")
        raw_permission_plan = planned.get("permission_plan")
        product_manager_build_workflow = planned.get("build_workflow")
        if not isinstance(blueprint, dict) or not isinstance(raw_permission_plan, dict):
            raise AgentWorkflowError("ProductManager proceeded without complete planning artifacts")
        product_manager_build_workflow = str(product_manager_build_workflow)
        workflow_override = CodexRoutingService(self.db).project_build_workflow_override()
        build_workflow = workflow_override or product_manager_build_workflow
        get_project_build_workflow(build_workflow)
        blueprint_path = self.artifacts.write_json(agent_run, "blueprint.json", blueprint)
        permission_plan = self._apply_blueprint_permission_plan(
            generation_request,
            {**blueprint, "permission_plan": raw_permission_plan},
        )
        permission_path = self.artifacts.write_json(agent_run, "permissions.json", permission_plan)
        archive_warning = self.codex_service.archive_product_manager_thread(generation_request)
        self._finish_step(
            agent_run,
            step,
            "succeeded",
            output_json={
                "decision_json": {"decision": "proceed_to_approval"},
                "decision_path": decision_path,
                "blueprint_json": blueprint,
                "permission_plan": permission_plan,
                "build_workflow": build_workflow,
                "product_manager_build_workflow": product_manager_build_workflow,
                "build_workflow_source": "settings_override" if workflow_override else "product_manager",
                "blueprint_path": blueprint_path,
                "permission_path": permission_path,
                "thread_archive_warning": archive_warning,
            },
            logs="ProductManager wrote blueprint.json and permissions.json without task nodes or tests. Permissions were not approved.",
        )

        building_skill = self._create_or_update_building_skill(generation_request, blueprint)
        generation_request.proposed_skill_id = building_skill.id
        generation_request.status = "awaiting_approval"
        generation_request.error_message = None
        agent_run.skill_id = building_skill.id
        agent_run.user_request = generation_request.user_message
        agent_run.current_task_id = None
        agent_run.current_step = "product_manager"
        agent_run.failure_count_json = {}
        agent_run.blueprint_json = blueprint
        agent_run.build_workflow = build_workflow
        agent_run.summary = "ProductManager created the skill blueprint and permission plan."
        self.db.commit()
        self.db.refresh(agent_run)

        pm_summary = self.codex_service.product_manager_summary(
            "build_time",
            {
                "intent_prompt": intent_prompt,
                "blueprint_json": blueprint,
                "permission_plan": permission_plan,
                "generation_request_id": generation_request.id,
            },
            self._pm_build_time_summary(blueprint),
        )
        permission_request = PermissionService(self.db, project_root=self.project_root).create_build_time_request(
            generation_request
        )
        permission_summary = self._permission_build_time_summary(generation_request.plan_json, permission_request)
        self._apply_combined_permission_summary(permission_request, pm_summary, permission_summary)
        self._finish_step(
            agent_run,
            self._start_backend_step(
                agent_run,
                "backend_build_time_permission_review",
                "Backend created the build-time permission review and is waiting for the local user's decision.",
                approval_request_id=permission_request.id,
            ),
            "waiting_for_approval",
            logs="Backend created the build-time permission review and is waiting for the local user's decision.",
        )
        agent_run.status = "waiting_for_approval"
        agent_run.current_step = "backend"
        agent_run.summary = "Waiting for one build-time approval."
        agent_run.final_summary_json = {
            "blueprint_path": blueprint_path,
            "permission_path": permission_path,
            "permission_request_id": permission_request.id,
            "user_summary": pm_summary,
        }
        self.db.commit()
        self.db.refresh(agent_run)
        return agent_run

    def continue_build_after_approval(self, generation_request: SkillGenerationRequest) -> tuple[AgentRun, Skill, object]:
        agent_run = self.latest_run_for_generation(generation_request.id) or self.create_build_run(generation_request)
        self._ensure_not_cancelled(agent_run)
        decision = PermissionService(self.db, project_root=self.project_root).can_generate(generation_request)
        if not decision.allowed:
            agent_run.status = "waiting_for_approval"
            agent_run.current_step = "backend"
            self.db.commit()
            raise AgentWorkflowError(decision.reason)

        self._mark_waiting_permission_steps_approved(agent_run)
        permission_plan = self._finalize_build_permission_plan(agent_run, generation_request)
        try:
            self._provision_build_dependencies(agent_run, generation_request, permission_plan)
            workflow = get_project_build_workflow(agent_run.build_workflow or DEFAULT_BUILD_WORKFLOW)
            return workflow.execute(self, generation_request, agent_run, permission_plan)
        except Exception as exc:
            if agent_run.status != "blocked":
                self._fail_run(agent_run, str(exc))
            raise

    def _provision_build_dependencies(
        self,
        agent_run: AgentRun,
        generation_request: SkillGenerationRequest,
        permission_plan: dict[str, Any],
    ) -> None:
        skill = self.db.get(Skill, generation_request.proposed_skill_id)
        if skill is None:
            raise AgentWorkflowError("Approved build no longer has a controlled skill record")
        self.proposed_service.prepare_generation_workspace(skill.name)
        step = self._start_backend_step(
            agent_run,
            "backend_build_dependency_provisioning",
            "Backend is provisioning and verifying the approved dependency environment.",
        )
        try:
            BuildDependencyService(self.db, project_root=self.project_root).provision(
                generation_request,
                skill,
                permission_plan,
            )
        except BuildDependencyError as exc:
            self._finish_step(
                agent_run,
                step,
                "failed",
                logs="Backend dependency provisioning failed before Codex started.",
                error_message=str(exc),
            )
            generation_request.status = "failed"
            generation_request.error_message = str(exc)
            self.db.commit()
            raise AgentWorkflowError(f"Build dependency provisioning failed before Codex started: {exc}") from exc
        self._finish_step(
            agent_run,
            step,
            "succeeded",
            logs="Backend provisioned and verified the controlled dependency environment before Codex started.",
        )

    def create_repair_run(self, skill: Skill, user_request: str | None = None) -> AgentRun:
        with SkillOperationGuard(self.db).locked(skill, "repair", reason="Agent repair workflow"):
            return self._create_repair_run_locked(skill, user_request)

    def _create_repair_run_locked(self, skill: Skill, user_request: str | None = None) -> AgentRun:
        if skill.status == "deleted":
            raise AgentWorkflowError("Deleted skills cannot be repaired")

        blueprint = self.codex_service.product_manager_repair_blueprint(skill, user_request)
        agent_run = AgentRun(
            run_type="repair_skill",
            status="running",
            skill_id=skill.id,
            user_request=user_request or f"Repair skill {skill.name}.",
            current_task_id="repair_skill",
            current_step="product_manager",
            failure_count_json={"repair_skill": 0},
            blueprint_json=blueprint,
            summary="ProductManager created a repair blueprint.",
        )
        self.db.add(agent_run)
        self.db.commit()
        self.db.refresh(agent_run)
        self.artifacts.initialize(agent_run)

        try:
            repair_skill = self._prepare_repair_target(skill, agent_run)
            pm_summary = self.codex_service.product_manager_summary(
                "repair_start",
                {"skill_id": skill.id, "repair_skill_name": repair_skill.name, "blueprint_json": blueprint},
                f"Repair proposed skill {repair_skill.name} using existing failure context and tests.",
            )
            self._finish_step(
                agent_run,
                self._start_step(
                    agent_run,
                    "product_manager",
                    task_node_id="repair_skill",
                    input_json={"skill_id": skill.id},
                    logs="ProductManager reviewed the repair request and selected repair mode.",
                ),
                "succeeded",
                output_json={
                    "blueprint_json": blueprint,
                    "decision_json": {"decision": "repair_current_task"},
                    "user_summary": pm_summary,
                },
                logs=pm_summary,
            )

            validation = self._repair_current_task(agent_run, repair_skill, None, task_node_id="repair_skill")
            while not validation.ok:
                self._increment_failure_count(agent_run, "repair_skill")
                if self._failure_count(agent_run, "repair_skill") > MAX_TASK_FAILURES:
                    self._product_manager_stop_failed(agent_run, repair_skill, validation)
                    return agent_run
                validation = self._repair_current_task(agent_run, repair_skill, validation, task_node_id="repair_skill")

            self._product_manager_after_tests(agent_run, repair_skill, validation, task_node_id="repair_skill")
            runtime_status = self._runtime_permission_review(agent_run, repair_skill, validation, task_node_id="repair_skill")
            self._product_manager_finish(agent_run, repair_skill, validation, runtime_status, task_node_id="repair_skill")
        except Exception as exc:
            if agent_run.status != "blocked":
                self._fail_run(agent_run, str(exc))
            raise
        return agent_run

    def create_update_run(self, skill: Skill, suggestion: str) -> AgentRun:
        update_review = self.codex_service.product_manager_update_review(skill, suggestion)
        decision = {
            "decision": str(update_review["decision"]),
            "summary": str(update_review["summary"]),
        }
        blueprint = update_review["blueprint"]
        agent_run = AgentRun(
            run_type="update_skill",
            status="running",
            skill_id=skill.id,
            user_request=suggestion,
            current_task_id="update_version",
            current_step="product_manager",
            failure_count_json={"update_version": 0},
            blueprint_json=blueprint,
            summary="ProductManager evaluated the update suggestion.",
        )
        self.db.add(agent_run)
        self.db.commit()
        self.db.refresh(agent_run)
        self.artifacts.initialize(agent_run)
        blueprint_path = self.artifacts.write_json(agent_run, "blueprint.json", blueprint)
        permission_plan = self._permission_plan_from_blueprint(blueprint)
        permission_path = self.artifacts.write_json(agent_run, "permissions.json", permission_plan)

        pm_status = "succeeded" if decision["decision"] in {"build_next_milestone", "request_permission"} else "blocked"
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                task_node_id="update_version",
                input_json={
                    "skill_id": skill.id,
                    "suggestion": suggestion,
                    "permission_policy": planning_permission_policy(),
                },
                logs="ProductManager evaluated the improvement suggestion against the current skill and project rules.",
            ),
            pm_status,
            output_json={
                "decision_json": decision,
                "blueprint_json": blueprint,
                "permission_plan": permission_plan,
                "blueprint_path": blueprint_path,
                "permission_path": permission_path,
                "user_summary": decision["summary"],
            },
            logs=decision["summary"],
            error_message=None,
        )

        if decision["decision"] == "request_permission":
            permission_request = PermissionService(self.db, project_root=self.project_root).create_update_build_time_request(
                skill,
                agent_run,
                blueprint,  # type: ignore[arg-type]
                decision["summary"],
            )
            permission_summary = self._permission_build_time_summary(blueprint, permission_request)  # type: ignore[arg-type]
            self._apply_combined_permission_summary(permission_request, decision["summary"], permission_summary)
            self._finish_step(
                agent_run,
                self._start_backend_step(
                    agent_run,
                    "backend_update_build_time_permission_review",
                    "Backend created the update build-time permission review and is waiting for the local user's decision.",
                    task_node_id="update_version",
                    approval_request_id=permission_request.id,
                ),
                "waiting_for_approval",
                logs=(
                    "Backend created the update build-time permission review and is waiting for the local user's decision."
                ),
            )
            agent_run.status = "waiting_for_approval"
            agent_run.current_step = "backend"
            agent_run.summary = decision["summary"]
            agent_run.final_summary_json = {
                "permission_request_id": permission_request.id,
                "blueprint_path": blueprint_path,
                "permission_path": permission_path,
                "user_summary": decision["summary"],
            }
            agent_run.error_message = None
            self.db.commit()
            self.db.refresh(agent_run)
            return agent_run

        if decision["decision"] != "build_next_milestone":
            agent_run.status = "blocked"
            agent_run.completed_at = utc_now()
            agent_run.summary = decision["summary"]
            agent_run.final_summary_json = {"user_summary": decision["summary"], "decision_json": decision}
            agent_run.error_message = None
            self.db.commit()
            self.db.refresh(agent_run)
            return agent_run

        return self._execute_update_run(agent_run, skill, suggestion, blueprint, decision["summary"])  # type: ignore[arg-type]

    def _execute_update_run(
        self,
        agent_run: AgentRun,
        skill: Skill,
        suggestion: str,
        blueprint: dict[str, Any],
        summary: str,
    ) -> AgentRun:
        version_service = SkillVersionService(self.db, project_root=self.project_root)
        permission_bounds = self._permission_bounds_for_run(agent_run, skill)
        agent_run.status = "running"
        agent_run.current_step = "builder"
        agent_run.error_message = None
        self.db.commit()
        try:
            draft = version_service.create_draft_from_active(skill, summary, created_by="agent")
            builder_step = self._start_step(
                agent_run,
                "builder",
                task_node_id="update_version",
                input_json={
                    "skill_id": skill.id,
                    "version_id": draft.id,
                    "suggestion": suggestion,
                    "blueprint_json": blueprint,
                    "blueprint_path": self.artifacts.relative_path(agent_run, "blueprint.json"),
                    "permission_path": self.artifacts.relative_path(agent_run, "permissions.json"),
                    "permission_bounds": permission_bounds,
                    "project_files": self._version_file_snapshot(draft),
                },
                logs="Builder is modifying only the copied draft version folder.",
            )
            result = self.codex_service.update_skill_version(
                skill,
                draft,
                suggestion,
                blueprint,
                permission_bounds,
            )
            self._finish_step(
                agent_run,
                builder_step,
                "succeeded",
                output_json={"version_id": draft.id, "stdout": result.stdout, "stderr": result.stderr},
                logs="Builder updated the draft version. The active version was not modified.",
            )

            validation = self._test_version_task(agent_run, skill, draft, version_service, blueprint)
            while not validation.ok:
                self._increment_failure_count(agent_run, "update_version")
                if self._failure_count(agent_run, "update_version") > MAX_TASK_FAILURES:
                    self._product_manager_stop_update_failed(agent_run, skill, draft, validation)
                    self.db.refresh(agent_run)
                    return agent_run
                validation = self._repair_update_version(agent_run, skill, draft, version_service, validation, blueprint)

            permission_request = version_service.create_runtime_request_if_needed(skill, draft)
            if permission_request is None:
                final_status = "succeeded"
                summary = self.codex_service.product_manager_summary(
                    "update_complete",
                    {"skill_id": skill.id, "version_id": draft.id, "permissions_changed": False},
                    f"Version {draft.version} is ready to compare and activate. Runtime permissions are unchanged.",
                )
            else:
                permission_summary = self._permission_runtime_summary(skill, permission_request)
                self.artifacts.write_json(
                    agent_run,
                    f"runtime_permissions_v{draft.id}.json",
                    {
                        "version_id": draft.id,
                        "permission_request_id": permission_request.id,
                        "permissions_changed": True,
                        "status": permission_request.status,
                        "permissions": permission_request.requested_permissions_json,
                    },
                )
                final_status = "succeeded"
                summary = self.codex_service.product_manager_summary(
                    "update_runtime_permission_required",
                    {"skill_id": skill.id, "version_id": draft.id, "permissions_changed": True},
                    f"Version {draft.version} is ready, but runtime permission approval is required before activation.",
                )
                self._apply_combined_permission_summary(permission_request, summary, permission_summary)

            self._finish_step(
                agent_run,
                self._start_backend_step(
                    agent_run,
                    "backend_finalize_update",
                    "Backend finalized the validated draft update and recorded its activation requirements.",
                    task_node_id="update_version",
                ),
                "succeeded",
                logs="Backend finalized the validated draft update and recorded its activation requirements.",
            )
            agent_run.status = final_status
            agent_run.summary = summary
            agent_run.final_summary_json = {
                "version_id": draft.id,
                "user_summary": summary,
                "runtime_permission_request_id": permission_request.id if permission_request is not None else None,
            }
            agent_run.completed_at = utc_now() if final_status == "succeeded" else None
            self.db.commit()
            self.db.refresh(agent_run)
            return agent_run
        except (SkillVersionError, Exception) as exc:
            if agent_run.status not in {"blocked", "failed"}:
                self._fail_run(agent_run, str(exc))
            raise

    def resume_run(self, agent_run: AgentRun) -> AgentRun:
        self._ensure_not_cancelled(agent_run)
        if agent_run.status not in {"waiting_for_approval", "pending", "paused", "failed"}:
            return agent_run
        if agent_run.status == "paused":
            below_reserve, usage = codex_usage_service.should_pause_workflow(CODEX_USAGE_RESERVE_PERCENT)
            if below_reserve:
                agent_run.pause_reason = self._usage_pause_message(usage)
                self.db.commit()
                raise AgentWorkflowError(agent_run.pause_reason)
            if agent_run.run_type == "build_skill" and agent_run.generation_request_id:
                workflow = get_project_build_workflow(agent_run.build_workflow or DEFAULT_BUILD_WORKFLOW)
                return workflow.resume(self, agent_run)
        if agent_run.status == "failed" and agent_run.run_type == "build_skill":
            workflow = get_project_build_workflow(agent_run.build_workflow or DEFAULT_BUILD_WORKFLOW)
            return workflow.retry_failed(self, agent_run)
        if agent_run.generation_request_id:
            generation_request = self.db.get(SkillGenerationRequest, agent_run.generation_request_id)
            if generation_request is None:
                raise AgentWorkflowError("Generation request no longer exists")
            if generation_request.status != "approved":
                decision = PermissionService(self.db, project_root=self.project_root).can_generate(generation_request)
                if decision.allowed:
                    generation_request.status = "approved"
                    agent_run.error_message = None
                    self.db.commit()
            self.continue_build_after_approval(generation_request)
            self.db.refresh(agent_run)
            return agent_run
        if agent_run.run_type == "update_skill" and agent_run.skill_id:
            skill = self.db.get(Skill, agent_run.skill_id)
            if skill is None:
                raise AgentWorkflowError("Skill no longer exists")
            request = self._latest_update_build_time_request(agent_run)
            if request is None:
                raise AgentWorkflowError("Update build-time permission request is missing")
            if request.status != "approved":
                raise AgentWorkflowError(f"Update build-time permission request is {request.status}")
            self._mark_waiting_permission_steps_approved(agent_run)
            self._execute_update_run(
                agent_run,
                skill,
                agent_run.user_request,
                agent_run.blueprint_json or {},
                agent_run.summary or agent_run.user_request,
            )
            self.db.refresh(agent_run)
            return agent_run
        return agent_run

    def _pause_if_usage_below_reserve(self, agent_run: AgentRun) -> bool:
        if not getattr(self.codex_service.adapter, "uses_codex_account_quota", False):
            return False
        below_reserve, usage = codex_usage_service.should_pause_workflow(CODEX_USAGE_RESERVE_PERCENT)
        if not below_reserve:
            return False
        agent_run.status = "paused"
        agent_run.pause_reason = self._usage_pause_message(usage)
        agent_run.summary = agent_run.pause_reason
        summary = dict(agent_run.final_summary_json or {})
        summary["usage_pause"] = usage
        agent_run.final_summary_json = summary
        self.db.commit()
        return True

    @staticmethod
    def _usage_pause_message(usage: dict[str, Any]) -> str:
        windows = [window for window in (usage.get("five_hour"), usage.get("weekly")) if window]
        low_window = next(
            (window for window in windows if window.get("remaining_percent", 100) < CODEX_USAGE_RESERVE_PERCENT),
            None,
        )
        if low_window:
            reset = low_window.get("resets_at") or "the allowance reset"
            return (
                f"Workflow paused because the Codex {low_window.get('label')} allowance has less than "
                f"{CODEX_USAGE_RESERVE_PERCENT}% remaining. Resets at {reset}."
            )
        return "Workflow paused because the Codex account allowance is at its limit."

    def retry_current_task(self, agent_run: AgentRun) -> AgentRun:
        self._ensure_not_cancelled(agent_run)
        if (
            agent_run.run_type == "build_skill"
            and agent_run.build_workflow == "single_codex"
            and agent_run.status in {"failed", "blocked"}
        ):
            raise AgentWorkflowError(
                "Single-Codex builds cannot be retried after an error. Start a new Project build instead."
            )
        if agent_run.status == "failed":
            return self.resume_run(agent_run)
        if agent_run.run_type == "build_skill" and agent_run.generation_request_id:
            generation_request = self.db.get(SkillGenerationRequest, agent_run.generation_request_id)
            if generation_request is None:
                raise AgentWorkflowError("Generation request no longer exists")
            generation_request.status = "approved"
            self.db.commit()
            self.continue_build_after_approval(generation_request)
            self.db.refresh(agent_run)
            return agent_run
        if agent_run.run_type == "repair_skill" and agent_run.skill_id:
            skill = self.db.get(Skill, agent_run.skill_id)
            if skill is None:
                raise AgentWorkflowError("Skill no longer exists")
            self.create_repair_run(skill, agent_run.user_request)
            return agent_run
        raise AgentWorkflowError("Retry current task is not available for this run")

    def retry_step(self, agent_run: AgentRun, step: AgentRunStep) -> AgentRun:
        if step.agent_run_id != agent_run.id:
            raise AgentWorkflowError("Step does not belong to this agent run")
        return self.retry_current_task(agent_run)

    def cancel_run(self, agent_run: AgentRun) -> AgentRun:
        clarification_wait = (
            agent_run.status == "blocked"
            and (agent_run.final_summary_json or {}).get("decision_json", {}).get("decision")
            == "ask_user_for_input"
        )
        if agent_run.status in {"succeeded", "failed", "cancelled"} or (
            agent_run.status == "blocked" and not clarification_wait
        ):
            return agent_run
        agent_run.status = "cancelled"
        agent_run.completed_at = utc_now()
        agent_run.error_message = "Cancelled by local user."
        if agent_run.generation_request_id:
            generation_request = self.db.get(SkillGenerationRequest, agent_run.generation_request_id)
            if generation_request is not None:
                archive_warning = self.codex_service.archive_product_manager_thread(generation_request)
                generation_request.status = "cancelled"
                generation_request.error_message = "Cancelled by local user."
                if archive_warning:
                    final_summary = dict(agent_run.final_summary_json or {})
                    final_summary["thread_archive_warning"] = archive_warning
                    agent_run.final_summary_json = final_summary
        for step in agent_run.steps:
            if step.status in {"pending", "running", "waiting_for_approval"}:
                step.status = "cancelled"
                step.ended_at = utc_now()
        self.db.commit()
        self.db.refresh(agent_run)
        return agent_run

    def latest_run_for_generation(self, generation_request_id: int) -> AgentRun | None:
        return self.db.scalar(
            select(AgentRun)
            .where(AgentRun.generation_request_id == generation_request_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        )

    def _latest_update_build_time_request(self, agent_run: AgentRun) -> ApprovalRequest | None:
        if agent_run.skill_id is None:
            return None
        requests = self.db.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.skill_id == agent_run.skill_id)
            .where(ApprovalRequest.request_scope == "build_time")
            .where(ApprovalRequest.request_type == "update")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        ).all()
        for request in requests:
            if (request.reason_json or {}).get("agent_run_id") == agent_run.id:
                return request
        return None

    def _test_task_node(self, agent_run: AgentRun, skill: Skill, validation: Any, task_node_id: str = DEFAULT_TASK_ID) -> Any:
        task_node = self._task_by_id(agent_run, task_node_id)
        tester_context = {
            "task_node": self._tester_task_node(task_node),
            "permission_bounds": self._permission_bounds_for_run(agent_run, skill),
            "function_context": self._function_context(task_node.get("function_ids", [])),
            "integration_test_adapter": "deterministic_fake",
            "parent_interface_artifacts": self.artifacts.direct_parent_interface_artifacts(agent_run, task_node),
            "test_file": f"tests/test_{task_node_id}.py",
            "workspace_paths": self._task_relevant_paths(task_node),
        }
        tester_step = self._start_step(
            agent_run,
            "tester",
            task_node_id=task_node_id,
            input_json=tester_context,
            logs="TesterAgent uses Codex to inspect the task contract and Builder code, write tests, then validate through the existing safe path.",
        )
        tester_generation: dict[str, Any]
        try:
            tester_result = self.codex_service.write_tests_for_skill(
                skill,
                tester_context,
                build_workflow=agent_run.build_workflow,
            )
            tests_written = self._existing_test_paths(skill)
            tester_generation = {
                "stdout": tester_result.stdout,
                "stderr": tester_result.stderr,
                "exit_code": tester_result.returncode,
            }
            validation = self.proposed_service.validate_proposed_skill(skill)
        except CodexGenerationError as exc:
            tests_written = self._existing_test_paths(skill)
            tester_generation = {"stdout": "", "stderr": str(exc), "exit_code": 1}
            validation = ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                error_message=f"TesterAgent failed to write tests: {exc}",
            )
        if validation.manifest_valid:
            skill_dir = self.proposed_service.skill_dir_for_record(skill)
            self.codex_service.update_skill_record_from_manifest(skill, skill_dir)
        self._finish_step(
            agent_run,
            tester_step,
            "succeeded" if validation.ok else "failed",
            output_json={
                "test_result_json": validation.model_dump(mode="json"),
                "failure_log": validation.error_message or validation.stderr or "",
                "tests_written": tests_written,
                "tester_generation": tester_generation,
            },
            logs="TesterAgent completed Codex-backed test writing and validation. No skill task was run automatically.",
            error_message=validation.error_message,
        )
        if not validation.ok:
            self._write_failure_log(agent_run, task_node_id, validation)
        self.artifacts.write_task_test_result(agent_run, task_node_id, validation)
        return validation

    def _run_final_validation(self, agent_run: AgentRun, skill: Skill) -> ProposedSkillValidationRead:
        agent_run.current_task_id = "final_e2e"
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        manifest_failure: ProposedSkillValidationRead | None = None
        manifest_runtime: dict[str, Any] = {}
        declared_integration_operations: set[str] = set()
        try:
            manifest = validate_manifest_file(skill_dir / "manifest.json")
            manifest_runtime = manifest.permissions.model_dump(mode="json")
            declared_integration_operations = {
                operation_id
                for requirement in manifest.integration_requirements
                for operation_id in requirement.operations
            }
            approved_integration_operations = set(self._approved_integration_operation_ids(agent_run))
            if declared_integration_operations - approved_integration_operations:
                raise ManifestValidationError(
                    "Manifest declares integration operations outside the approved build context: "
                    f"{sorted(declared_integration_operations - approved_integration_operations)}"
                )
            planned_runtime = str(self._agent_blueprint(agent_run).get("runtime") or "function")
            if manifest.runtime != planned_runtime:
                raise ManifestValidationError(
                    f"Manifest runtime {manifest.runtime!r} does not match approved blueprint runtime {planned_runtime!r}"
                )
            if manifest.name != skill.name:
                raise ManifestValidationError(
                    f"Manifest name {manifest.name!r} does not match the controlled skill name {skill.name!r}"
                )
        except (FileNotFoundError, ManifestValidationError, OSError) as exc:
            manifest_failure = ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                tests_run=False,
                tests_passed=None,
                error_message=str(exc),
            )
        scan = StaticCapabilityScanner().scan(
            skill_dir,
            manifest_runtime,
            runtime=manifest.runtime if manifest_failure is None else "function",
            declared_integration_operations=declared_integration_operations,
            selected_integration_operations=set(self._approved_integration_operation_ids(agent_run)),
        )
        if manifest_failure is None and agent_run.build_workflow == "task_dag":
            scan = self._merge_task_integration_context_scans(
                agent_run,
                skill_dir,
                manifest_runtime,
                manifest.runtime,
                declared_integration_operations,
                scan,
            )
        scan_payload = scan.model_dump()
        self.artifacts.write_json(agent_run, "capability_scan.json", scan_payload)
        if manifest_failure is not None:
            validation = manifest_failure
        elif not scan.ok:
            validation = self._capability_scan_failure(scan)
        else:
            validation = self.proposed_service.validate_proposed_skill(skill)
            if validation.manifest_valid:
                self.codex_service.update_skill_record_from_manifest(skill, skill_dir)
        output = {
            "test_result_json": validation.model_dump(mode="json"),
            "failure_log": validation.error_message or validation.stderr or "",
            "tests_written": self._existing_test_paths(skill),
            "capability_scan": scan_payload,
        }
        self.artifacts.write_json(agent_run, "final_e2e_test_result.json", output)
        if not validation.ok:
            self.artifacts.write_text(
                agent_run,
                "final_e2e_failure.log",
                self._validation_log_text("final_e2e", validation),
            )
        return validation

    def _merge_task_integration_context_scans(
        self,
        agent_run: AgentRun,
        skill_dir: Path,
        manifest_runtime: dict[str, Any],
        runtime: str,
        declared_operations: set[str],
        full_scan: CapabilityScanResult,
    ) -> CapabilityScanResult:
        ordered_nodes = self.task_dags.topological_nodes(self._task_dag(agent_run))
        path_owner: dict[str, str] = {}
        for node in ordered_nodes:
            for raw_path in node.get("write_paths", []) or []:
                path_owner[str(raw_path).replace("\\", "/").removeprefix("./")] = str(node["id"])
        scanned_files = set(full_scan.scanned_files)
        findings = list(full_scan.findings)
        scanner = StaticCapabilityScanner()
        for node in ordered_nodes:
            node_id = str(node["id"])
            owned_paths = {path for path, owner in path_owner.items() if owner == node_id}
            if not owned_paths:
                continue
            node_scan = scanner.scan(
                skill_dir,
                manifest_runtime,
                runtime=runtime,
                declared_integration_operations=declared_operations,
                selected_integration_operations=set(
                    FunctionCatalogService(
                        self.db,
                        project_root=self.project_root,
                    ).integration_operation_ids(node.get("function_ids", []))
                ),
                include_paths=owned_paths,
            )
            scanned_files.update(node_scan.scanned_files)
            findings.extend(
                finding
                for finding in node_scan.findings
                if finding.capability == "integration_operation"
            )
        unique = {
            (finding.capability, finding.status, finding.path, finding.line, finding.evidence): finding
            for finding in findings
        }
        merged_findings = sorted(
            unique.values(),
            key=lambda finding: (finding.path, finding.line, finding.capability, finding.evidence),
        )
        return CapabilityScanResult(
            ok=not any(finding.blocking for finding in merged_findings),
            scanned_files=sorted(scanned_files),
            findings=merged_findings,
            limitations=full_scan.limitations,
        )

    def _capability_scan_failure(self, scan: CapabilityScanResult) -> ProposedSkillValidationRead:
        blocking = [finding for finding in scan.findings if finding.blocking]
        details = "; ".join(
            f"{finding.path}:{finding.line} {finding.capability}: {finding.message}" for finding in blocking[:10]
        )
        if len(blocking) > 10:
            details += f"; and {len(blocking) - 10} more finding(s)"
        return ProposedSkillValidationRead(
            ok=False,
            manifest_valid=True,
            tests_run=False,
            tests_passed=None,
            error_message=f"Static capability scan found undeclared or blocked behavior. {details}",
        )

    def _write_final_e2e_test(self, agent_run: AgentRun, skill: Skill) -> None:
        tester_context = {
            "blueprint_contract": self._final_blueprint_contract(agent_run),
            "permission_bounds": self._permission_bounds_for_run(agent_run, skill),
            "acceptance_criteria": self._final_acceptance_criteria(agent_run),
            "interface_contracts": self._final_interface_contracts(agent_run, skill),
            "test_file": "tests/test_final_e2e.py",
            "workspace_paths": self._skill_file_paths(skill),
            "function_context": self._function_context(self._agent_blueprint(agent_run).get("functions", [])),
            "integration_test_adapter": "deterministic_fake",
        }
        tester_step = self._start_step(
            agent_run,
            "tester",
            task_node_id="final_e2e",
            input_json=tester_context,
            logs="TesterAgent writes and runs the final end-to-end validation.",
        )
        try:
            tester_result = self.codex_service.write_tests_for_skill(
                skill,
                tester_context,
                build_workflow=agent_run.build_workflow,
            )
            tests_written = self._existing_test_paths(skill)
            tester_generation = {
                **self._subprocess_output(tester_result),
                "exit_code": tester_result.returncode,
            }
        except CodexGenerationError as exc:
            tests_written = self._existing_test_paths(skill)
            tester_generation = {"stdout": "", "stderr": str(exc), "exit_code": 1}
            self._finish_step(
                agent_run,
                tester_step,
                "failed",
                output_json={"tests_written": tests_written, "tester_generation": tester_generation},
                logs="TesterAgent failed to write the final end-to-end test.",
                error_message=str(exc),
            )
            raise AgentWorkflowError(f"TesterAgent failed to write final end-to-end tests: {exc}") from exc
        self._finish_step(
            agent_run,
            tester_step,
            "succeeded",
            output_json={"tests_written": tests_written, "tester_generation": tester_generation},
            logs="TesterAgent wrote the final end-to-end test. Backend validation runs separately.",
        )

    def _repair_final_e2e(self, agent_run: AgentRun, skill: Skill, validation: Any) -> Any:
        context = {
            "action": "builder_fix_final_e2e",
            "permission_bounds": self._agent_permission_bounds(self._permission_plan(agent_run)),
            "blueprint_contract": self._final_blueprint_contract(agent_run),
            "acceptance_criteria": self._final_acceptance_criteria(agent_run),
            "interface_contracts": self._final_interface_contracts(agent_run, skill),
            "failure": self._validation_failure_context("final_e2e", validation),
            "workspace_paths": self._skill_file_paths(skill),
            "function_context": self._function_context(self._agent_blueprint(agent_run).get("functions", [])),
        }
        builder_step = self._start_step(
            agent_run,
            "builder",
            task_node_id="final_e2e",
            input_json={"mode": "fix_final_e2e", "failure_context": context},
            logs="Builder is repairing cross-node final end-to-end failures.",
        )
        try:
            result = self.codex_service.repair_skill(
                skill,
                context,
                build_workflow=agent_run.build_workflow,
            )
        except CodexGenerationError as exc:
            if "USER_ACTION_REQUIRED:" in str(exc):
                self._builder_user_action_required(agent_run, builder_step, str(exc), "final_e2e")
                raise AgentWorkflowError(str(exc)) from exc
            raise
        self._finish_step(
            agent_run,
            builder_step,
            "succeeded",
            output_json={**self._subprocess_output(result), "exit_code": result.returncode, "mode": "fix_final_e2e"},
            logs="Builder repaired final end-to-end integration issues.",
        )
        return self._run_final_validation(agent_run, skill)

    def _test_version_task(
        self,
        agent_run: AgentRun,
        skill: Skill,
        draft: Any,
        version_service: SkillVersionService,
        blueprint: dict[str, Any],
    ) -> Any:
        code_files = self._version_file_snapshot(draft)
        tester_context = {
            "mode": "update",
            "permission_bounds": self._permission_bounds_for_run(agent_run, skill),
            "skill_id": skill.id,
            "version_id": draft.id,
            "blueprint_json": blueprint,
            "milestone": self._milestone_by_name(agent_run, "update_version"),
            "code_files": code_files,
            "blueprint_path": self.artifacts.relative_path(agent_run, "blueprint.json"),
            "permission_path": self.artifacts.relative_path(agent_run, "permissions.json"),
            "responsibility": "Tester writes or updates tests for the draft version, then validates manifest and test results.",
            "function_context": self._function_context(blueprint.get("functions", [])),
            "integration_test_adapter": "deterministic_fake",
        }
        tester_step = self._start_step(
            agent_run,
            "tester",
            task_node_id="update_version",
            input_json=tester_context,
            logs="TesterAgent uses Codex to inspect the update blueprint and draft code, write tests, then validate through the existing safe path.",
        )
        try:
            tester_result = self.codex_service.write_tests_for_version(skill, draft, tester_context)
            tester_generation = {
                "stdout": tester_result.stdout,
                "stderr": tester_result.stderr,
                "exit_code": tester_result.returncode,
            }
            validation = version_service.validate_version(draft)
            if validation.ok:
                validation = self._validate_version_integration_context(draft, blueprint, validation)
        except CodexGenerationError as exc:
            tester_generation = {"stdout": "", "stderr": str(exc), "exit_code": 1}
            validation = ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                error_message=f"TesterAgent failed to write draft-version tests: {exc}",
            )
        self._finish_step(
            agent_run,
            tester_step,
            "succeeded" if validation.ok else "failed",
            output_json={
                "version_id": draft.id,
                "test_result_json": validation.model_dump(mode="json"),
                "failure_log": validation.error_message or validation.stderr or "",
                "tests_written": self._version_test_paths(draft),
                "tester_generation": tester_generation,
            },
            logs="TesterAgent completed Codex-backed draft-version test writing and validation.",
            error_message=validation.error_message,
        )
        self.artifacts.write_text(
            agent_run,
            "update_version_test.log",
            self._validation_log_text("update_version", validation),
        )
        return validation

    def _validate_version_integration_context(
        self,
        draft: Any,
        blueprint: dict[str, Any],
        validation: ProposedSkillValidationRead,
    ) -> ProposedSkillValidationRead:
        version_dir = (self.project_root / draft.folder_path).resolve()
        try:
            manifest = validate_manifest_file(version_dir / "manifest.json")
        except (FileNotFoundError, ManifestValidationError, OSError) as exc:
            return ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                tests_run=validation.tests_run,
                tests_passed=validation.tests_passed,
                error_message=str(exc),
            )
        selected_operations = {
            str(operation_id)
            for requirement in blueprint.get("integration_requirements", []) or []
            if isinstance(requirement, dict)
            for operation_id in requirement.get("operations", []) or []
        }
        declared_operations = {
            operation_id
            for requirement in manifest.integration_requirements
            for operation_id in requirement.operations
        }
        if declared_operations - selected_operations:
            return ProposedSkillValidationRead(
                ok=False,
                manifest_valid=True,
                tests_run=validation.tests_run,
                tests_passed=validation.tests_passed,
                error_message=(
                    "Draft manifest declares integration operations outside the approved update context: "
                    f"{sorted(declared_operations - selected_operations)}"
                ),
            )
        scan = StaticCapabilityScanner().scan(
            version_dir,
            manifest.permissions.model_dump(mode="json"),
            runtime=manifest.runtime,
            declared_integration_operations=declared_operations,
            selected_integration_operations=selected_operations,
        )
        return validation if scan.ok else self._capability_scan_failure(scan)

    def _repair_update_version(
        self,
        agent_run: AgentRun,
        skill: Skill,
        draft: Any,
        version_service: SkillVersionService,
        validation: Any,
        blueprint: dict[str, Any],
    ) -> Any:
        context = {
            "user_request": agent_run.user_request,
            "permission_bounds": self._permission_bounds_for_run(agent_run, skill),
            "skill_id": skill.id,
            "version_id": draft.id,
            "blueprint_json": blueprint,
            "milestone": self._milestone_by_name(agent_run, "update_version"),
            "test_result_json": validation.model_dump(mode="json"),
            "failure_log_path": self.artifacts.relative_path(agent_run, "update_version_test.log"),
            "project_files": self._version_file_snapshot(draft),
            "function_context": self._function_context(blueprint.get("functions", [])),
        }
        builder_step = self._start_step(
            agent_run,
            "builder",
            task_node_id="update_version",
            input_json={
                "mode": "repair",
                "skill_id": skill.id,
                "version_id": draft.id,
                "blueprint_json": blueprint,
                "failure_context": context,
            },
            logs="Builder is repairing the draft version using Tester failure output.",
        )
        try:
            result = self.codex_service.repair_skill_version(skill, draft, context)
        except CodexGenerationError as exc:
            if "USER_ACTION_REQUIRED:" in str(exc):
                self._builder_user_action_required(agent_run, builder_step, str(exc), "update_version")
                raise AgentWorkflowError(str(exc)) from exc
            raise
        self._finish_step(
            agent_run,
            builder_step,
            "succeeded",
            output_json={"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode, "mode": "repair"},
            logs="Builder repaired the draft version. The active version was not modified.",
        )
        return self._test_version_task(agent_run, skill, draft, version_service, blueprint)

    def _repair_current_task(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any | None,
        task_node_id: str = DEFAULT_TASK_ID,
    ) -> Any:
        task_node = self._task_by_id(agent_run, task_node_id)
        context = {
            **self._builder_task_context(agent_run, task_node, skill),
            "action": "builder_fix_task",
            "current_interface_artifact": self.artifacts.task_interface_artifact(agent_run, task_node_id),
            "failure": self._validation_failure_context(task_node_id, validation) if validation is not None else None,
        }
        builder_step = self._start_step(
            agent_run,
            "builder",
            task_node_id=task_node_id,
            input_json={
                "mode": "fix_task",
                "action": "builder_fix_task",
                "failure_context": context,
            },
            logs="Builder is repairing the current task node using Tester failure output.",
        )
        try:
            result = self.codex_service.repair_skill(
                skill,
                context,
                build_workflow=agent_run.build_workflow,
            )
        except CodexGenerationError as exc:
            if "USER_ACTION_REQUIRED:" in str(exc):
                self._builder_user_action_required(agent_run, builder_step, str(exc), task_node_id)
                raise AgentWorkflowError(str(exc)) from exc
            raise
        self._finish_step(
            agent_run,
            builder_step,
            "succeeded",
            output_json={**self._subprocess_output(result), "exit_code": result.returncode, "mode": "fix_task"},
            logs="Builder proposed a repair. The skill was not installed or run.",
        )
        self.artifacts.move_validated_interface_artifact(
            agent_run,
            self.proposed_service.skill_dir_for_record(skill),
            task_node,
        )
        return self._test_task_node(agent_run, skill, None, task_node_id=task_node_id)

    def _product_manager_after_tests(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any,
        task_node_id: str = DEFAULT_TASK_ID,
    ) -> None:
        self._finish_step(
            agent_run,
            self._start_backend_step(
                agent_run,
                "backend_record_task_validation",
                "Backend recorded that the current task passed validation and selected the next workflow state.",
                task_node_id=task_node_id,
            ),
            "succeeded",
            logs="Backend recorded that the current task passed validation and selected the next workflow state.",
        )

    def _runtime_permission_review(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any,
        task_node_id: str = DEFAULT_TASK_ID,
        pm_summary: str | None = None,
    ) -> str:
        runtime_request = PermissionService(self.db, project_root=self.project_root).create_runtime_request(skill)
        permission_summary = self._permission_runtime_summary(skill, runtime_request)
        if pm_summary is None:
            pm_summary = self.codex_service.product_manager_summary(
                "runtime_review_checkpoint",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "validation_ok": validation.ok,
                    "runtime_permission_status": runtime_request.status,
                },
                self._pm_runtime_summary(skill, validation),
            )
        self._apply_combined_permission_summary(runtime_request, pm_summary, permission_summary)
        self.artifacts.write_json(
            agent_run,
            "runtime_permissions.json",
            {
                "skill_id": skill.id,
                "permission_request_id": runtime_request.id,
                "status": runtime_request.status,
                "risk_level": runtime_request.risk_level,
                "permission_expansion": runtime_request.reason_json.get("permission_expansion", {}),
                "runner_unsupported": runtime_request.reason_json.get("runner_unsupported", []),
                "function_requirements": runtime_request.reason_json.get("function_requirements", []),
                "integration_requirements": runtime_request.reason_json.get("integration_requirements", []),
                "permissions": runtime_request.requested_permissions_json,
                "product_manager_summary": pm_summary,
                "permission_review_summary": permission_summary,
            },
        )
        return runtime_request.status

    def _product_manager_finish(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any,
        runtime_status: str,
        task_node_id: str = DEFAULT_TASK_ID,
        pm_summary: str | None = None,
    ) -> None:
        summary = pm_summary or self.codex_service.product_manager_summary(
                "completion",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "validation_ok": validation.ok,
                    "runtime_permission_status": runtime_status,
                    "blueprint_json": agent_run.blueprint_json,
                },
                (
                    f"Skill {skill.name} is ready for user review. Validation passed; runtime permission request is {runtime_status}. "
                    "The skill remains proposed and was not installed or run automatically."
                ),
            )
        previous_final_summary = dict(agent_run.final_summary_json or {})
        final_summary = {
            "skill_id": skill.id,
            "skill_name": skill.name,
            "validation_ok": validation.ok,
            "runtime_permission_status": runtime_status,
            "user_summary": summary,
        }
        if "task_statuses" in previous_final_summary:
            final_summary["task_statuses"] = previous_final_summary["task_statuses"]
        self._finish_step(
            agent_run,
            self._start_backend_step(
                agent_run,
                "backend_finalize_build",
                "Backend finalized validation, recorded runtime permission requirements, and marked the build ready for review.",
                task_node_id=task_node_id,
            ),
            "succeeded",
            logs=(
                "Backend finalized validation, recorded runtime permission requirements, and marked the build ready for review."
            ),
        )
        agent_run.skill_id = skill.id
        agent_run.status = "succeeded"
        skill.status = "proposed"
        agent_run.current_step = "backend"
        agent_run.summary = summary
        agent_run.final_summary_json = final_summary
        agent_run.completed_at = utc_now()
        agent_run.error_message = None
        self.db.commit()
        self.db.refresh(agent_run)

    def _product_manager_stop_failed(self, agent_run: AgentRun, skill: Skill, validation: Any) -> None:
        task_node_id = agent_run.current_task_id or DEFAULT_TASK_ID
        is_final = task_node_id == "final_e2e"
        failure_label = "Final end-to-end validation" if is_final else f"Task node {task_node_id}"
        summary = self.codex_service.product_manager_summary(
            "stop_failed",
            {
                "skill_id": skill.id,
                "task_id": task_node_id,
                "failure_count_json": agent_run.failure_count_json,
                "latest_failure": validation.error_message or validation.stderr or "tests failed",
            },
            (
                f"{failure_label} failed more than {MAX_TASK_FAILURES} times. "
                f"Latest failure: {validation.error_message or 'tests failed'}. User review is recommended."
            ),
        )
        self._finish_step(
            agent_run,
            self._start_backend_step(
                agent_run,
                "backend_stop_failed_build",
                "Backend stopped the workflow after the bounded validation failure limit was reached.",
                task_node_id=task_node_id,
            ),
            "blocked",
            logs="Backend stopped the workflow after the bounded validation failure limit was reached.",
            error_message=summary,
        )
        agent_run.status = "blocked"
        skill.status = "failed"
        agent_run.summary = summary
        agent_run.error_message = summary
        agent_run.completed_at = utc_now()
        agent_run.final_summary_json = {"user_summary": summary, "failure_count_json": agent_run.failure_count_json}
        if agent_run.generation_request_id:
            generation_request = self.db.get(SkillGenerationRequest, agent_run.generation_request_id)
            if generation_request is not None:
                generation_request.status = "failed"
                generation_request.error_message = validation.error_message or "Skill tests failed"
        self.db.commit()

    def _product_manager_stop_update_failed(
        self,
        agent_run: AgentRun,
        skill: Skill,
        draft: Any,
        validation: Any,
    ) -> None:
        summary = self.codex_service.product_manager_summary(
            "update_stop_failed",
            {
                "skill_id": skill.id,
                "version_id": draft.id,
                "task_node_id": "update_version",
                "failure_count_json": agent_run.failure_count_json,
                "latest_failure": validation.error_message or validation.stderr or "tests failed",
            },
            (
                f"Update version {draft.version} failed more than {MAX_TASK_FAILURES} times. "
                "The active version was not modified. User review is recommended."
            ),
        )
        self._finish_step(
            agent_run,
            self._start_backend_step(
                agent_run,
                "backend_stop_failed_update",
                "Backend stopped the update after the bounded validation failure limit was reached.",
                task_node_id="update_version",
            ),
            "failed",
            logs="Backend stopped the update after the bounded validation failure limit was reached.",
            error_message=summary,
        )
        agent_run.status = "failed"
        agent_run.summary = summary
        agent_run.error_message = summary
        agent_run.completed_at = utc_now()
        agent_run.final_summary_json = {
            "version_id": draft.id,
            "user_summary": summary,
            "failure_count_json": agent_run.failure_count_json,
        }
        self.db.commit()

    def _builder_user_action_required(
        self,
        agent_run: AgentRun,
        builder_step: AgentRunStep,
        message: str,
        task_node_id: str,
    ) -> None:
        report = {
            "exact_blocker": message.replace("USER_ACTION_REQUIRED:", "").strip(),
            "why_builder_cannot_proceed_safely": "The blocker requires user action or unsupported capability.",
            "specific_user_step_needed": "Review the blocker and adjust requirements or provide the missing approved resource.",
            "workflow_can_resume": True,
            "files_or_permissions_involved": [],
        }
        self._finish_step(
            agent_run,
            builder_step,
            "blocked",
            output_json={"user_action_required": report},
            logs=report["exact_blocker"],
            error_message=report["exact_blocker"],
        )
        agent_run.status = "blocked"
        agent_run.current_task_id = task_node_id
        agent_run.current_step = "builder"
        agent_run.summary = report["exact_blocker"]
        agent_run.final_summary_json = {"user_action_required": report}
        agent_run.completed_at = utc_now()
        self._mark_linked_skill_failed(agent_run, report["exact_blocker"])
        self.db.commit()

    def _prepare_repair_target(self, skill: Skill, agent_run: AgentRun) -> Skill:
        if skill.status == "proposed":
            return skill
        if skill.status != "installed":
            raise AgentWorkflowError("Only proposed or installed skills can be repaired")
        repair_name = self.proposed_service.validate_skill_name(f"{skill.name}_repair_{agent_run.id}")
        source_dir = self.proposed_service.skill_dir_for_record(skill)
        repair_dir = self.proposed_service.proposed_dir(repair_name)
        if repair_dir.exists():
            shutil.rmtree(repair_dir)
        shutil.copytree(
            source_dir,
            repair_dir,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        manifest_path = repair_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = repair_name
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        repair_skill = Skill(
            name=repair_name,
            description=f"Repair proposal for {skill.name}.",
            runtime=skill.runtime,
            status="proposed",
            risk_level=skill.risk_level,
            manifest_path=self.proposed_service._relative_path(manifest_path),
            instructions_path=skill.instructions_path,
            input_schema_json=skill.input_schema_json,
            output_schema_json=skill.output_schema_json,
            function_requirements_json=list(skill.function_requirements_json or []),
            integration_requirements_json=list(skill.integration_requirements_json or []),
            installed_path=None,
            enabled=False,
        )
        self.db.add(repair_skill)
        self.db.commit()
        self.db.refresh(repair_skill)
        return repair_skill

    def _create_or_update_building_skill(
        self,
        generation_request: SkillGenerationRequest,
        blueprint: dict[str, Any],
    ) -> Skill:
        plan = generation_request.plan_json
        skill_name = self.proposed_service.validate_skill_name(plan["skill_name"])
        proposed_dir = self.proposed_service.proposed_dir(skill_name)
        skill = self.db.scalar(select(Skill).where(Skill.name == skill_name))
        values = {
            "description": plan.get("description") or generation_request.user_message,
            "runtime": plan.get("runtime", "function"),
            "status": "building",
            "risk_level": plan["risk_level"],
            "manifest_path": self.proposed_service._relative_path(proposed_dir / "manifest.json"),
            "instructions_path": self._planned_instructions_path(plan),
            "input_schema_json": plan.get("input_schema"),
            "output_schema_json": plan.get("output_schema"),
            "function_requirements_json": list(plan.get("function_requirements", []) or []),
            "integration_requirements_json": list(plan.get("integration_requirements", []) or []),
            "installed_path": None,
            "enabled": False,
        }
        if skill is None:
            skill = Skill(name=skill_name, **values)
            self.db.add(skill)
        elif skill.status in {"building", "failed"}:
            for key, value in values.items():
                setattr(skill, key, value)
        elif skill.status == "proposed":
            raise AgentWorkflowError(f"Proposed skill already exists: {skill_name}")
        elif skill.status == "installed":
            raise AgentWorkflowError(f"Installed skill already exists: {skill_name}")
        else:
            raise AgentWorkflowError(f"Skill name is not available: {skill_name}")
        self.db.commit()
        self.db.refresh(skill)
        return skill

    def _planned_instructions_path(self, plan: dict[str, Any]) -> str | None:
        files = plan.get("files_to_generate")
        if isinstance(files, list) and any(str(path).replace("\\", "/") == "SKILL.md" for path in files):
            return "SKILL.md"
        return None

    def _pm_build_time_summary(self, blueprint: dict[str, Any]) -> str:
        return (
            f"Build the {blueprint.get('name')} skill. "
            "ProductManager will define Builder-owned package files in the task DAG only after approval. "
            "Approval lets the backend provision listed dependencies and lets Codex generate proposed files; "
            "it does not install or run the skill."
        )

    def _permission_plan_from_blueprint(self, blueprint: dict[str, Any]) -> dict[str, Any]:
        value = blueprint.get("permission_plan")
        if not isinstance(value, dict):
            value = {}
        runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
        permissions = {
            "network": list(runtime.get("network", []) or []),
            "filesystem_read": [
                path
                for path in list(runtime.get("filesystem_read", []) or [])
                if str(path).replace("\\", "/").removeprefix("./").rstrip("/") != "cache"
            ],
            "filesystem_write": [
                path
                for path in list(runtime.get("filesystem_write", []) or [])
                if str(path).replace("\\", "/").removeprefix("./").rstrip("/") != "cache"
            ],
            "secrets": list(runtime.get("secrets", []) or []),
            "shell": bool(runtime.get("shell", False)),
            "codex": runtime.get("codex", {"call_response": True, "internet_access": bool(runtime.get("network"))}),
        }
        dependencies = list(runtime.get("dependencies", []) or [])
        build_time = value.get("build_time") if isinstance(value.get("build_time"), dict) else {}
        build_time_dependencies = [
            dependency
            for dependency in list(build_time.get("dependencies", dependencies) or [])
            if str(dependency).lower() not in default_build_time_dependencies()
        ]
        return {
            "build_time": {
                "internet_research": bool(build_time.get("internet_research", bool(permissions["network"] or dependencies))),
                "dependencies": build_time_dependencies,
            },
            "runtime": {
                **permissions,
                "dependencies": dependencies,
            },
        }

    def _apply_blueprint_permission_plan(
        self,
        generation_request: SkillGenerationRequest,
        blueprint: dict[str, Any],
    ) -> dict[str, Any]:
        permission_plan = self._permission_plan_from_blueprint(blueprint)
        runtime = permission_plan["runtime"]
        plan = dict(generation_request.plan_json or {})
        plan["blueprint_json"] = blueprint
        plan["permission_plan"] = permission_plan
        runtime_permissions = self._runtime_permissions_from_plan(permission_plan)
        function_catalog = FunctionCatalogService(self.db, project_root=self.project_root)
        selected_functions = function_catalog.validate_available_ids(blueprint.get("functions"))
        user_functions = function_catalog.user_function_names(selected_functions)
        integration_operations = function_catalog.integration_operation_ids(selected_functions)
        integration_requirements = []
        operations_by_provider: dict[str, list[str]] = {}
        for operation_id in integration_operations:
            provider = OPERATIONS[operation_id].provider
            operations_by_provider.setdefault(provider, []).append(operation_id)
        for provider, provider_operations in sorted(operations_by_provider.items()):
            integration_requirements.append(
                {
                    "provider": provider,
                    "operations": provider_operations,
                    "resource_scope": {"repositories": []} if provider == "github" else {},
                }
            )
        skill_runtime = str(blueprint.get("runtime") or "function")
        skill_name = self.proposed_service.validate_skill_name(str(blueprint["name"]))
        display_name = skill_name.replace("_", " ").replace("-", " ").title()
        plan.update(
            {
                "description": blueprint.get("description") or generation_request.user_message,
                "skill_name": skill_name,
                "display_name": display_name,
                "runtime": skill_runtime,
                "input_schema": blueprint.get("input_schema"),
                "output_schema": blueprint.get("output_schema"),
                "functions": selected_functions,
                "function_requirements": user_functions,
                "integration_requirements": integration_requirements,
                "schedule": blueprint.get("schedule"),
                "files_to_generate": [
                    "manifest.json",
                    "README.md",
                    "app.py" if skill_runtime == "web_app" else "skill.py",
                    "tests/test_app.py" if skill_runtime == "web_app" else "tests/test_skill.py",
                ],
            }
        )
        plan["requested_permissions"] = runtime_permissions
        plan["requested_network_domains"] = runtime_permissions["network"]
        plan["requested_dependencies"] = runtime["dependencies"]
        risk_level = classify_permission_risk(
            ManifestPermissions.model_validate(runtime_permissions),
            list(runtime["dependencies"]),
        )
        plan["risk_level"] = risk_level
        generation_request.plan_json = plan
        generation_request.requested_permissions_json = runtime_permissions
        generation_request.requested_dependencies_json = runtime["dependencies"]
        generation_request.requested_network_domains_json = runtime_permissions["network"]
        generation_request.proposed_skill_name = skill_name
        generation_request.proposed_display_name = display_name
        generation_request.risk_level = risk_level
        self.db.commit()
        self.db.refresh(generation_request)
        return permission_plan

    def _stop_final_validation_failed(self, agent_run: AgentRun, skill: Skill, validation: Any) -> None:
        message = validation.error_message or validation.stderr or "Final backend validation failed"
        summary = f"Final backend validation failed. {message}"
        agent_run.status = "blocked"
        agent_run.current_task_id = "final_e2e"
        agent_run.summary = summary
        agent_run.error_message = message
        agent_run.completed_at = utc_now()
        agent_run.final_summary_json = {
            "validation_ok": False,
            "backend_final_validation": "manifest_tests_and_capability_scan",
            "failure_count_json": agent_run.failure_count_json,
            "user_summary": summary,
        }
        skill.status = "failed"
        if agent_run.generation_request_id:
            generation_request = self.db.get(SkillGenerationRequest, agent_run.generation_request_id)
            if generation_request is not None:
                generation_request.status = "failed"
                generation_request.error_message = message
        self.db.commit()

    def _runtime_permissions_from_plan(self, permission_plan: dict[str, Any]) -> dict[str, Any]:
        runtime = permission_plan.get("runtime") if isinstance(permission_plan.get("runtime"), dict) else {}
        return {
            "network": list(runtime.get("network", []) or []),
            "filesystem_read": list(runtime.get("filesystem_read", []) or []),
            "filesystem_write": list(runtime.get("filesystem_write", []) or []),
            "secrets": list(runtime.get("secrets", []) or []),
            "shell": bool(runtime.get("shell", False)),
            "codex": runtime.get("codex", {"call_response": True, "internet_access": bool(runtime.get("network"))}),
        }

    def _finalize_build_permission_plan(
        self,
        agent_run: AgentRun,
        generation_request: SkillGenerationRequest,
    ) -> dict[str, Any]:
        raw_permission_plan = self.artifacts.read_json(agent_run, "permissions.json")
        final_permission_plan = effective_permission_plan(raw_permission_plan)
        self.artifacts.write_json(agent_run, "permissions.json", final_permission_plan)
        runtime = final_permission_plan["runtime"]
        runtime_permissions = self._runtime_permissions_from_plan(final_permission_plan)
        plan = dict(generation_request.plan_json or {})
        plan["permission_plan"] = final_permission_plan
        plan["requested_permissions"] = runtime_permissions
        plan["requested_network_domains"] = runtime_permissions["network"]
        plan["requested_dependencies"] = list(runtime.get("dependencies", []) or [])
        generation_request.plan_json = plan
        generation_request.requested_permissions_json = runtime_permissions
        generation_request.requested_dependencies_json = plan["requested_dependencies"]
        generation_request.requested_network_domains_json = runtime_permissions["network"]
        self.db.commit()
        self.db.refresh(generation_request)
        return final_permission_plan

    def _record_pending_product_manager_question(
        self,
        generation_request: SkillGenerationRequest,
        prompt: str,
    ) -> None:
        plan = dict(generation_request.plan_json or {})
        conversation = list(plan.get("project_conversation") or [])
        if not conversation:
            conversation.append({"role": "user", "content": generation_request.user_message})
        if not conversation or conversation[-1] != {"role": "assistant", "content": prompt}:
            conversation.append({"role": "assistant", "content": prompt})
        plan["project_conversation"] = conversation
        plan["pending_user_prompt"] = prompt
        generation_request.plan_json = plan
        self.db.commit()
        self.db.refresh(generation_request)

    def _permission_build_time_summary(self, plan: dict[str, Any], permission_request: Any) -> str:
        permissions = plan.get("requested_permissions", {})
        dependencies = plan.get("requested_dependencies", [])
        network = plan.get("requested_network_domains", [])
        return (
            f"Risk level {permission_request.risk_level}. Requested permissions: {permissions}. "
            f"Dependencies: {dependencies or []}. Network domains: {network or []}. "
            "Unsupported or blocked items remain blocked by the approval system."
        )

    def _pm_runtime_summary(self, skill: Skill, validation: Any) -> str:
        return (
            f"Generated skill {skill.name} appears {'complete' if validation.ok else 'incomplete'} after tests. "
            "It remains proposed until the user explicitly installs it."
        )

    def _permission_runtime_summary(self, skill: Skill, runtime_request: Any) -> str:
        expansion = runtime_request.reason_json.get("permission_expansion", {})
        expansion_text = f" Permission expansion: {expansion}." if expansion else " No permission expansion was detected."
        return (
            f"Actual manifest runtime risk is {runtime_request.risk_level}. "
            f"Permissions: {runtime_request.requested_permissions_json}. "
            f"Unsupported runner items: {runtime_request.reason_json.get('runner_unsupported', [])}."
            f"{expansion_text}"
        )

    def _apply_combined_permission_summary(self, permission_request: Any, pm_summary: str, permission_summary: str) -> None:
        permission_request.reason_json = {
            **(permission_request.reason_json or {}),
            "product_manager_summary": pm_summary,
            "permission_review_summary": permission_summary,
        }
        permission_request.user_explanation = f"ProductManager: {pm_summary}\n\nPermission review: {permission_summary}"
        permission_request.reason = permission_request.user_explanation
        self.db.commit()
        self.db.refresh(permission_request)

    def _mark_waiting_permission_steps_approved(self, agent_run: AgentRun) -> None:
        waiting_steps = [
            step
            for step in agent_run.steps
            if step.status == "waiting_for_approval" and step.approval_request_id is not None
        ]
        for step in waiting_steps:
            step.status = "succeeded"
            step.ended_at = utc_now()
            step.logs = f"{step.logs or ''}\n\nApproved by local user.".strip()
        self.db.commit()

    def _milestone_by_name(self, agent_run: AgentRun, name: str) -> dict[str, Any]:
        return self._task_by_id(agent_run, name)

    def _next_task_node_id(self, agent_run: AgentRun, current_name: str) -> str | None:
        nodes = self.task_dags.topological_nodes(self._task_dag(agent_run))
        for index, node in enumerate(nodes):
            if node["id"] == current_name:
                if index + 1 < len(nodes):
                    return nodes[index + 1]["id"]
                return None
        return None

    def _task_dag(self, agent_run: AgentRun) -> dict[str, Any]:
        payload = self.artifacts.read_json(agent_run, "task_dag.json")
        if payload:
            return payload
        return {
            "schema_version": 1,
            "nodes": [
                {
                    "id": agent_run.current_task_id or DEFAULT_TASK_ID,
                    "task_prompt": "Build the compatibility task node.",
                    "depends_on": [],
                    "difficulty": "medium",
                    "requires_tests": True,
                    "parallel_safe": True,
                    "write_paths": ["manifest.json"],
                    "acceptance_criteria": ["validation passes"],
                    "test_expectations": ["tests pass"],
                }
            ],
        }

    def _task_nodes(self, agent_run: AgentRun) -> list[dict[str, Any]]:
        return self.task_dags.nodes(self._task_dag(agent_run))

    def _task_by_id(self, agent_run: AgentRun, task_id: str) -> dict[str, Any]:
        for node in self._task_nodes(agent_run):
            if node.get("id") == task_id:
                return node
        return {
            "id": task_id,
            "task_prompt": "Build the requested task node.",
            "depends_on": [],
            "acceptance_criteria": [],
            "write_paths": ["manifest.json"],
            "requires_tests": True,
        }

    def _failure_count(self, agent_run: AgentRun, task_node_id: str) -> int:
        return int((agent_run.failure_count_json or {}).get(task_node_id, 0))

    def _increment_failure_count(self, agent_run: AgentRun, task_node_id: str) -> None:
        counts = dict(agent_run.failure_count_json or {})
        counts[task_node_id] = int(counts.get(task_node_id, 0)) + 1
        agent_run.failure_count_json = counts
        self.db.commit()

    def _write_failure_log(self, agent_run: AgentRun, task_node_id: str, validation: Any) -> None:
        log_dir = self.artifacts.directory(agent_run) / "tasks" / task_node_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "failure.log"
        content = self._validation_log_text(task_node_id, validation)
        log_path.write_text(content, encoding="utf-8")

    def _validation_log_text(self, task_node_id: str, validation: Any) -> str:
        return "\n".join(
            [
                f"task_id={task_node_id}",
                f"error={validation.error_message or ''}",
                "stdout:",
                validation.stdout or "",
                "stderr:",
                validation.stderr or "",
            ]
        )

    def _permission_plan(self, agent_run: AgentRun) -> dict[str, Any]:
        return self.artifacts.read_json(agent_run, "permissions.json")

    def _agent_blueprint(self, agent_run: AgentRun) -> dict[str, Any]:
        blueprint = dict(agent_run.blueprint_json or {})
        blueprint.pop("permission_plan", None)
        return blueprint

    def _builder_task_node(self, task_node: dict[str, Any]) -> dict[str, Any]:
        useful_fields = [
            "task_prompt",
            "write_paths",
            "acceptance_criteria",
        ]
        return {key: task_node[key] for key in useful_fields if key in task_node}

    def _tester_task_node(self, task_node: dict[str, Any]) -> dict[str, Any]:
        useful_fields = [
            "task_prompt",
            "acceptance_criteria",
            "test_expectations",
        ]
        return {key: task_node[key] for key in useful_fields if key in task_node}

    def _builder_task_context(
        self,
        agent_run: AgentRun,
        task_node: dict[str, Any],
        skill: Skill | None = None,
    ) -> dict[str, Any]:
        context = {
            "action": "builder_build_task",
            "permission_bounds": self._permission_bounds_for_run(agent_run, skill),
            "task_node": self._builder_task_node(task_node),
            "function_context": self._function_context(task_node.get("function_ids", [])),
            "parent_interface_artifacts": self.artifacts.direct_parent_interface_artifacts(agent_run, task_node),
            "workspace_paths": self._task_relevant_paths(task_node),
        }
        return context

    def _agent_permission_bounds(self, permission_plan: dict[str, Any]) -> dict[str, Any]:
        return agent_permission_bounds(permission_plan)

    def _permission_bounds_for_run(self, agent_run: AgentRun, skill: Skill | None = None) -> dict[str, Any]:
        permission_plan = self._permission_plan(agent_run)
        if permission_plan:
            return self._agent_permission_bounds(effective_permission_plan(permission_plan))
        if skill is not None:
            manifest_path = self.proposed_service.skill_dir_for_record(skill) / "manifest.json"
            try:
                manifest = validate_manifest_file(manifest_path)
            except (FileNotFoundError, ManifestValidationError, OSError):
                pass
            else:
                return self._agent_permission_bounds(
                    effective_permission_plan(
                        {
                            "build_time": {"dependencies": list(manifest.dependencies)},
                            "runtime": {
                                **manifest.permissions.model_dump(mode="json"),
                                "dependencies": list(manifest.dependencies),
                            },
                        }
                    )
                )
        return self._agent_permission_bounds(effective_permission_plan({}))

    def _final_blueprint_contract(self, agent_run: AgentRun) -> dict[str, Any]:
        blueprint = self._agent_blueprint(agent_run)
        fields = ("description", "runtime", "expected_behavior", "schedule")
        return {field: blueprint[field] for field in fields if field in blueprint}

    def _approved_integration_operation_ids(self, agent_run: AgentRun) -> list[str]:
        if agent_run.build_workflow == "task_dag":
            function_ids = [
                str(function_id)
                for node in self.task_dags.nodes(self._task_dag(agent_run))
                for function_id in node.get("function_ids", []) or []
            ]
        else:
            function_ids = list(self._agent_blueprint(agent_run).get("functions", []) or [])
        return FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).integration_operation_ids(function_ids)

    def _function_context(self, function_ids: object) -> list[dict[str, Any]]:
        return FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).context(function_ids)

    def _compact_interface_artifacts(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fields = ("task_id", "interfaces", "contracts_for_children", "known_limitations")
        return [{field: artifact[field] for field in fields if field in artifact} for artifact in artifacts]

    def _task_interface_artifact(self, agent_run: AgentRun, task_id: str) -> dict[str, Any]:
        return self.artifacts.task_interface_artifact(agent_run, task_id)

    def _validation_failure_context(self, task_id: str, validation: Any) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "error": validation.error_message or "",
            "stdout": self._truncate_text(validation.stdout or "", 4000),
            "stderr": self._truncate_text(validation.stderr or "", 4000),
        }

    def _downstream_intent_prompt(self, intent_prompt: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": int(intent_prompt.get("schema_version", 1)),
            "refined_prompt": str(intent_prompt.get("refined_prompt") or ""),
        }

    def _task_relevant_paths(self, task_node: dict[str, Any]) -> list[str]:
        paths = ["manifest.json"]
        for item in task_node.get("write_paths", []) or []:
            value = str(item)
            if value and value not in paths and not value.startswith("tests/"):
                paths.append(value)
        return paths

    def _final_acceptance_criteria(self, agent_run: AgentRun) -> list[str]:
        blueprint = self._agent_blueprint(agent_run)
        fallback: list[str] = []
        expected_behavior = blueprint.get("expected_behavior")
        if isinstance(expected_behavior, list):
            fallback.extend(str(item) for item in expected_behavior if str(item).strip())
        elif isinstance(expected_behavior, str) and expected_behavior.strip():
            fallback.append(expected_behavior)
        return list(dict.fromkeys(fallback))

    def _final_interface_contracts(self, agent_run: AgentRun, skill: Skill) -> list[dict[str, Any]]:
        artifacts = self._compact_interface_artifacts(self.artifacts.all_interface_artifacts(agent_run))
        if artifacts:
            return artifacts
        manifest_path = self.proposed_service.skill_dir_for_record(skill) / "manifest.json"
        manifest = {}
        if manifest_path.is_file():
            try:
                raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = raw_manifest if isinstance(raw_manifest, dict) else {}
            except json.JSONDecodeError:
                manifest = {}
        return [
            {
                "task_id": "single_codex",
                "interfaces": {
                    "entrypoint": manifest.get("entrypoint"),
                    "input_schema": manifest.get("input_schema") or {},
                    "output_schema": manifest.get("output_schema") or {},
                },
                "contracts_for_children": [
                    "The final package must satisfy the approved blueprint and manifest input/output contract."
                ],
                "known_limitations": [],
            }
        ]

    def _subprocess_output(self, result: Any, limit: int = 8000) -> dict[str, str]:
        return {
            "stdout": self._truncate_text(getattr(result, "stdout", "") or "", limit),
            "stderr": self._truncate_text(getattr(result, "stderr", "") or "", limit),
        }

    def _truncate_text(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        omitted = len(value) - limit
        return f"{value[:limit]}\n...[truncated {omitted} characters]"

    def _existing_test_paths(self, skill: Skill) -> list[str]:
        try:
            skill_dir = self.proposed_service.skill_dir_for_record(skill)
        except Exception:
            return []
        tests_dir = skill_dir / "tests"
        if not tests_dir.is_dir():
            return []
        paths = []
        for path in sorted(tests_dir.rglob("test_*.py")):
            paths.append(path.relative_to(skill_dir).as_posix())
        return paths

    def _skill_file_paths(self, skill: Skill) -> list[str]:
        try:
            skill_dir = self.proposed_service.skill_dir_for_record(skill)
        except Exception:
            return []
        excluded_names = {"interface_artifact.json", "codex_prompt.txt", "codex_last_message.txt"}
        excluded_parts = {".agents", ".build-deps", ".deps", ".git", ".pytest_cache", "__pycache__"}
        return [
            path.relative_to(skill_dir).as_posix()
            for path in sorted(skill_dir.rglob("*"))
            if path.is_file()
            and path.name not in excluded_names
            and not any(part in excluded_parts for part in path.parts)
        ]

    def _version_file_snapshot(self, version: Any) -> dict[str, str]:
        version_dir = (self.project_root / version.folder_path).resolve()
        return snapshot_skill_files(version_dir)

    def _version_test_paths(self, version: Any) -> list[str]:
        version_dir = (self.project_root / version.folder_path).resolve()
        tests_dir = version_dir / "tests"
        if not tests_dir.is_dir():
            return []
        return [path.relative_to(version_dir).as_posix() for path in sorted(tests_dir.rglob("test_*.py"))]

    def _start_step(
        self,
        agent_run: AgentRun,
        step_name: str,
        *,
        action: str | None = None,
        task_node_id: str | None = None,
        approval_request_id: int | None = None,
        input_json: dict[str, Any] | None = None,
        logs: str | None = None,
    ) -> AgentRunStep:
        self._ensure_not_cancelled(agent_run)
        recorded_action = action
        if recorded_action is None and input_json:
            candidate = input_json.get("action") or input_json.get("mode")
            recorded_action = str(candidate) if candidate else None
        step = AgentRunStep(
            agent_run_id=agent_run.id,
            step_name=step_name,
            action=recorded_action,
            task_node_id=task_node_id,
            approval_request_id=approval_request_id,
            status="running",
            input_json=input_json,
            logs=logs,
            started_at=utc_now(),
        )
        self.db.add(step)
        agent_run.status = "running"
        agent_run.current_step = step_name
        agent_run.current_task_id = task_node_id or agent_run.current_task_id
        self.db.commit()
        self.db.refresh(step)
        return step

    def _start_backend_step(
        self,
        agent_run: AgentRun,
        action: str,
        summary: str,
        *,
        task_node_id: str | None = None,
        approval_request_id: int | None = None,
    ) -> AgentRunStep:
        return self._start_step(
            agent_run,
            "backend",
            action=action,
            task_node_id=task_node_id,
            approval_request_id=approval_request_id,
            logs=summary,
        )

    def _finish_step(
        self,
        agent_run: AgentRun,
        step: AgentRunStep,
        status: str,
        *,
        output_json: dict[str, Any] | None = None,
        logs: str | None = None,
        error_message: str | None = None,
    ) -> None:
        invocations = self.codex_service.consume_invocation_usage()
        transcripts = self.codex_service.consume_agent_transcripts()
        if len(transcripts) > 1:
            raise AgentWorkflowError("One agent-run step cannot contain multiple Codex transcripts")
        if transcripts:
            if step.step_name == "backend":
                raise AgentWorkflowError("Backend steps cannot contain Codex transcripts")
            step.action = transcripts[0]["action"]
            step.agent_input_text = transcripts[0]["input"]
            step.agent_output_text = transcripts[0]["output"]
        if invocations:
            step.codex_invocations_json = invocations
            for field_name in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            ):
                value = sum(int(invocation.get(field_name, 0)) for invocation in invocations)
                setattr(step, field_name, value)
                run_field = f"total_{field_name}" if field_name != "total_tokens" else "total_tokens"
                setattr(agent_run, run_field, int(getattr(agent_run, run_field, 0) or 0) + value)
        step.status = status
        step.output_json = output_json
        step.logs = logs
        step.error_message = error_message
        step.ended_at = utc_now()
        agent_run.current_step = step.step_name
        self.db.commit()

    def _fail_run(self, agent_run: AgentRun, message: str) -> None:
        agent_run.status = "failed"
        agent_run.error_message = message
        agent_run.completed_at = utc_now()
        running_step = next((step for step in reversed(agent_run.steps) if step.status == "running"), None)
        if running_step is not None:
            running_step.status = "failed"
            running_step.error_message = message
            running_step.ended_at = utc_now()
        self._mark_linked_skill_failed(agent_run, message)
        self.db.commit()

    def _mark_linked_skill_failed(self, agent_run: AgentRun, message: str) -> None:
        if agent_run.skill_id:
            skill = self.db.get(Skill, agent_run.skill_id)
            if skill is not None and skill.status in {"building", "proposed", "failed"}:
                skill.status = "failed"
        if agent_run.generation_request_id:
            generation_request = self.db.get(SkillGenerationRequest, agent_run.generation_request_id)
            if generation_request is not None and generation_request.status not in {"cancelled"}:
                generation_request.status = "failed"
                generation_request.error_message = message

    def _ensure_not_cancelled(self, agent_run: AgentRun) -> None:
        if agent_run.status == "cancelled":
            raise AgentWorkflowError("Agent run is cancelled")
