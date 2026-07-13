import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentRunStep, ApprovalRequest, MemoryFact, Skill, SkillGenerationRequest
from app.schemas.interface_artifact import InterfaceArtifact
from app.schemas.proposed_skill import ProposedSkillValidationRead
from app.services.backend_api_catalog import (
    backend_api_context,
    valid_backend_api_ids,
)
from app.services.codex_routing_service import CodexRoutingService
from app.services.codex_service import CodexGenerationError, CodexService
from app.services.codex_usage_service import codex_usage_service
from app.services.default_permissions import (
    default_banned_permissions,
    default_build_time_dependencies,
    effective_permission_plan,
)
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_operation_guard import SkillOperationGuard
from app.services.skill_version_service import SkillVersionError, SkillVersionService
from app.workflows.base import (
    DEFAULT_BUILD_WORKFLOW,
    MAX_TASK_FAILURES,
    ProjectBuildWorkflowError,
)
from app.workflows.registry import get_project_build_workflow

AgentWorkflowError = ProjectBuildWorkflowError


DEFAULT_TASK_ID = "core_skill"
CODEX_USAGE_RESERVE_PERCENT = 5


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
        if self.codex_service is None:
            self.codex_service = CodexService(self.db, project_root=self.project_root)

    def create_build_run(self, generation_request: SkillGenerationRequest) -> AgentRun:
        existing = self.latest_run_for_generation(generation_request.id)
        if existing and existing.status in {"pending", "running", "waiting_for_approval", "paused", "succeeded"}:
            return existing
        if (
            existing
            and existing.status == "blocked"
            and (existing.final_summary_json or {}).get("decision_json", {}).get("decision") == "ask_user_for_input"
        ):
            return self._review_build_intent(generation_request, existing)

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
        self._initialize_artifact_dir(agent_run)
        return self._review_build_intent(generation_request, agent_run)

    def _review_build_intent(self, generation_request: SkillGenerationRequest, agent_run: AgentRun) -> AgentRun:
        agent_run.status = "running"
        agent_run.completed_at = None
        agent_run.error_message = None
        agent_run.user_request = generation_request.user_message
        self.db.commit()

        selected_memory_facts = self._selected_memory_facts()
        intent_prompt = self.codex_service.product_manager_refine_intent(generation_request, selected_memory_facts)
        intent_path = self._write_json_artifact(agent_run, "intent_prompt.json", intent_prompt)
        intent_step = self._start_step(
            agent_run,
            "product_manager",
            input_json={
                "action": "pm_refine_intent",
                "user_request": generation_request.user_message,
                "project_conversation": generation_request.plan_json.get("project_conversation", []),
                "selected_memory_facts": selected_memory_facts,
            },
            logs="ProductManager refined the Project-mode request before plausibility review.",
        )
        self._finish_step(
            agent_run,
            intent_step,
            "succeeded",
            output_json={"intent_prompt": intent_prompt, "intent_prompt_path": intent_path},
            logs="ProductManager wrote intent_prompt.json.",
        )

        downstream_intent = self._downstream_intent_prompt(intent_prompt)
        review = self.codex_service.product_manager_build_review(generation_request, downstream_intent)
        decision = str(review["decision"])
        user_prompt = review.get("user_prompt")
        summary = (
            str(user_prompt)
            if user_prompt
            else "ProductManager confirmed that the request is ready for blueprinting."
        )
        decision_json = {"decision": decision, "user_prompt": user_prompt}
        decision_path = self._write_json_artifact(agent_run, "decision.json", decision_json)
        step = self._start_step(
            agent_run,
            "product_manager",
            input_json={
                "action": "pm_review_plausibility",
                "intent_prompt": downstream_intent,
            },
            logs="ProductManager reviewed clarity, plausibility, and MVP support before blueprint creation.",
        )

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
            self._finish_step(
                agent_run,
                step,
                "blocked",
                output_json={
                    "decision_json": {"decision": "stop_inplausible"},
                    "decision_path": decision_path,
                    "user_summary": summary,
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
            }
            agent_run.completed_at = utc_now()
            self.db.commit()
            self.db.refresh(agent_run)
            return agent_run

        self._finish_step(
            agent_run,
            step,
            "succeeded",
            output_json={
                "decision_json": {"decision": "proceed_to_blueprint"},
                "decision_path": decision_path,
                "user_summary": summary,
            },
            logs=summary,
        )
        return self._create_build_artifacts_after_review(generation_request, agent_run, downstream_intent)

    def _create_build_artifacts_after_review(
        self,
        generation_request: SkillGenerationRequest,
        agent_run: AgentRun,
        intent_prompt: dict[str, Any],
    ) -> AgentRun:
        blueprint, raw_permission_plan, product_manager_build_workflow = (
            self.codex_service.product_manager_write_blueprint_and_permissions(
                generation_request,
                intent_prompt,
            )
        )
        workflow_override = CodexRoutingService(self.db).project_build_workflow_override()
        build_workflow = workflow_override or product_manager_build_workflow
        get_project_build_workflow(build_workflow)
        blueprint_path = self._write_json_artifact(agent_run, "blueprint.json", blueprint)
        permission_plan = self._apply_blueprint_permission_plan(
            generation_request,
            {**blueprint, "permission_plan": raw_permission_plan},
        )
        permission_path = self._write_json_artifact(agent_run, "permissions.json", permission_plan)
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                input_json={
                    "action": "pm_write_blueprint_and_permissions",
                    "intent_prompt": intent_prompt,
                },
                logs="ProductManager wrote the skill blueprint and permission plan.",
            ),
            "succeeded",
            output_json={
                "blueprint_json": blueprint,
                "permission_plan": permission_plan,
                "build_workflow": build_workflow,
                "product_manager_build_workflow": product_manager_build_workflow,
                "build_workflow_source": "settings_override" if workflow_override else "product_manager",
                "blueprint_path": blueprint_path,
                "permission_path": permission_path,
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
            self._start_step(
                agent_run,
                "product_manager",
                input_json={
                    "action": "backend_build_time_permission_review",
                    "blueprint_json": blueprint,
                    "permission_plan": permission_plan,
                },
                logs="Backend created the deterministic build-time approval request from blueprint.json and permissions.json.",
            ),
            "waiting_for_approval",
            output_json={
                "blueprint_json": blueprint,
                "permission_plan": permission_plan,
                "blueprint_path": blueprint_path,
                "permission_path": permission_path,
                "permission_request_id": permission_request.id,
                "risk_level": permission_request.risk_level,
                "decision_json": {
                    "decision": "request_permission",
                    "reason": "Build-time approval is required before Codex writes proposed files.",
                },
                "user_summary": pm_summary,
                "permission_review_summary": permission_summary,
            },
            logs=f"ProductManager: {pm_summary}\n\nPermission review: {permission_summary}",
        )
        agent_run.status = "waiting_for_approval"
        agent_run.current_step = "product_manager"
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
            agent_run.current_step = "product_manager"
            self.db.commit()
            raise AgentWorkflowError(decision.reason)

        self._mark_waiting_permission_steps_approved(agent_run)
        permission_plan = self._finalize_build_permission_plan(agent_run, generation_request)
        workflow = get_project_build_workflow(agent_run.build_workflow or DEFAULT_BUILD_WORKFLOW)
        try:
            return workflow.execute(self, generation_request, agent_run, permission_plan)
        except Exception as exc:
            if agent_run.status != "blocked":
                self._fail_run(agent_run, str(exc))
            raise

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
        self._initialize_artifact_dir(agent_run)

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
        self._initialize_artifact_dir(agent_run)
        blueprint_path = self._write_json_artifact(agent_run, "blueprint.json", blueprint)
        permission_plan = self._permission_plan_from_blueprint(blueprint)
        permission_path = self._write_json_artifact(agent_run, "permissions.json", permission_plan)

        pm_status = "succeeded" if decision["decision"] in {"build_next_milestone", "request_permission"} else "blocked"
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                task_node_id="update_version",
                input_json={"skill_id": skill.id, "suggestion": suggestion},
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
            pm_step = agent_run.steps[-1]
            pm_step.status = "waiting_for_approval"
            pm_step.ended_at = utc_now()
            pm_step.output_json = {
                **(pm_step.output_json or {}),
                "permission_request_id": permission_request.id,
                "risk_level": permission_request.risk_level,
                "permission_review_summary": permission_summary,
            }
            pm_step.logs = f"{pm_step.logs or decision['summary']}\n\nPermission review: {permission_summary}"
            agent_run.status = "waiting_for_approval"
            agent_run.current_step = "product_manager"
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
                    "blueprint_path": self._artifact_relative_path(agent_run, "blueprint.json"),
                    "permission_path": self._artifact_relative_path(agent_run, "permissions.json"),
                    "project_files": self._version_file_snapshot(draft),
                },
                logs="Builder is modifying only the copied draft version folder.",
            )
            result = self.codex_service.update_skill_version(skill, draft, suggestion, blueprint)
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
                self._write_json_artifact(
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
                self._start_step(
                    agent_run,
                    "product_manager",
                    task_node_id="update_version",
                    input_json={"skill_id": skill.id, "version_id": draft.id},
                    logs="ProductManager summarized the proposed update.",
                ),
                "succeeded",
                output_json={"version_id": draft.id, "user_summary": summary},
                logs=summary,
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
        if agent_run.status in {"succeeded", "failed", "cancelled", "blocked"}:
            return agent_run
        agent_run.status = "cancelled"
        agent_run.completed_at = utc_now()
        agent_run.error_message = "Cancelled by local user."
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
            "parent_interface_artifacts": self._direct_parent_interface_artifacts(agent_run, task_node),
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
        self._write_task_test_result(agent_run, task_node_id, validation)
        return validation

    def _test_final_e2e(self, agent_run: AgentRun, skill: Skill, validation: Any) -> Any:
        tester_context = {
            "blueprint_contract": self._final_blueprint_contract(agent_run),
            "final_e2e_expectations": self._final_e2e_expectations(agent_run),
            "interface_contracts": self._compact_interface_artifacts(self._all_interface_artifacts(agent_run)),
            "test_file": "tests/test_final_e2e.py",
            "workspace_paths": self._skill_file_paths(skill),
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
            validation = self.proposed_service.validate_proposed_skill(skill)
        except CodexGenerationError as exc:
            tests_written = self._existing_test_paths(skill)
            tester_generation = {"stdout": "", "stderr": str(exc), "exit_code": 1}
            validation = ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                error_message=f"TesterAgent failed to write final end-to-end tests: {exc}",
            )
        if validation.manifest_valid:
            skill_dir = self.proposed_service.skill_dir_for_record(skill)
            self.codex_service.update_skill_record_from_manifest(skill, skill_dir)
        output = {
            "test_result_json": validation.model_dump(mode="json"),
            "failure_log": validation.error_message or validation.stderr or "",
            "tests_written": tests_written,
            "tester_generation": tester_generation,
        }
        self._finish_step(
            agent_run,
            tester_step,
            "succeeded" if validation.ok else "failed",
            output_json=output,
            logs="TesterAgent completed final end-to-end test writing and validation.",
            error_message=validation.error_message,
        )
        self._write_json_artifact(agent_run, "final_e2e_test_result.json", output)
        if not validation.ok:
            self._write_text_artifact(agent_run, "final_e2e_failure.log", self._validation_log_text("final_e2e", validation))
        return validation

    def _repair_final_e2e(self, agent_run: AgentRun, skill: Skill, validation: Any) -> Any:
        context = {
            "action": "builder_fix_final_e2e",
            "permission_bounds": self._agent_permission_bounds(self._permission_plan(agent_run)),
            "blueprint_contract": self._final_blueprint_contract(agent_run),
            "final_e2e_expectations": self._final_e2e_expectations(agent_run),
            "interface_contracts": self._compact_interface_artifacts(self._all_interface_artifacts(agent_run)),
            "failure": self._validation_failure_context("final_e2e", validation),
            "workspace_paths": self._skill_file_paths(skill),
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
        return self._test_final_e2e(agent_run, skill, validation)

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
            "skill_id": skill.id,
            "version_id": draft.id,
            "blueprint_json": blueprint,
            "milestone": self._milestone_by_name(agent_run, "update_version"),
            "code_files": code_files,
            "blueprint_path": self._artifact_relative_path(agent_run, "blueprint.json"),
            "permission_path": self._artifact_relative_path(agent_run, "permissions.json"),
            "responsibility": "Tester writes or updates tests for the draft version, then validates manifest and test results.",
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
        self._write_text_artifact(
            agent_run,
            "update_version_test.log",
            self._validation_log_text("update_version", validation),
        )
        return validation

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
            "skill_id": skill.id,
            "version_id": draft.id,
            "blueprint_json": blueprint,
            "milestone": self._milestone_by_name(agent_run, "update_version"),
            "test_result_json": validation.model_dump(mode="json"),
            "failure_log_path": self._artifact_relative_path(agent_run, "update_version_test.log"),
            "project_files": self._version_file_snapshot(draft),
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
            "current_interface_artifact": self._task_interface_artifact(agent_run, task_node_id),
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
        self._move_validated_interface_artifact(agent_run, skill, task_node)
        return self._test_task_node(agent_run, skill, None, task_node_id=task_node_id)

    def _product_manager_after_tests(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any,
        task_node_id: str = DEFAULT_TASK_ID,
    ) -> None:
        next_task_node_id = self._next_task_node_id(agent_run, task_node_id)
        decision = "finish_ready_for_review" if next_task_node_id is None else "build_next_milestone"
        summary = self.codex_service.product_manager_summary(
            "milestone_passed",
            {
                "skill_id": skill.id,
                "task_node_id": task_node_id,
                "next_task_node_id": next_task_node_id,
                "test_result_json": validation.model_dump(mode="json"),
                "blueprint_json": agent_run.blueprint_json,
            },
            (
                f"Milestone {task_node_id} passed. " +
                (
                    "ProductManager considers the planned blueprint complete and is preparing runtime permission review."
                    if next_task_node_id is None
                    else f"ProductManager is continuing to milestone {next_task_node_id}."
                )
            ),
        )
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                task_node_id=task_node_id,
                input_json={"skill_id": skill.id, "test_result_json": validation.model_dump(mode="json")},
                logs="ProductManager reviewed the passing milestone result.",
            ),
            "succeeded",
            output_json={"decision_json": {"decision": decision}, "user_summary": summary},
            logs=summary,
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
        self._write_json_artifact(
            agent_run,
            "runtime_permissions.json",
            {
                "skill_id": skill.id,
                "permission_request_id": runtime_request.id,
                "status": runtime_request.status,
                "risk_level": runtime_request.risk_level,
                "permission_expansion": runtime_request.reason_json.get("permission_expansion", {}),
                "runner_unsupported": runtime_request.reason_json.get("runner_unsupported", []),
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
            self._start_step(
                agent_run,
                "product_manager",
                task_node_id=task_node_id,
                input_json={"skill_id": skill.id},
                logs="ProductManager wrote the completion summary.",
            ),
            "succeeded",
            output_json={"decision_json": {"decision": "finish_ready_for_review"}, "user_summary": summary},
            logs=summary,
        )
        agent_run.skill_id = skill.id
        agent_run.status = "succeeded"
        skill.status = "proposed"
        agent_run.current_step = "product_manager"
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
            self._start_step(
                agent_run,
                "product_manager",
                task_node_id=task_node_id,
                input_json={"skill_id": skill.id, "failure_count_json": agent_run.failure_count_json},
                logs="ProductManager stopped the workflow after repeated failures.",
            ),
            "blocked",
            output_json={"decision_json": {"decision": "stop_failed"}, "user_summary": summary},
            logs=summary,
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
            self._start_step(
                agent_run,
                "product_manager",
                task_node_id="update_version",
                input_json={"skill_id": skill.id, "version_id": draft.id, "failure_count_json": agent_run.failure_count_json},
                logs="ProductManager stopped the update workflow after repeated draft-version failures.",
            ),
            "failed",
            output_json={"decision_json": {"decision": "stop_failed"}, "user_summary": summary},
            logs=summary,
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
        manifest["enabled"] = False
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        repair_skill = Skill(
            name=repair_name,
            description=f"Repair proposal for {skill.name}.",
            skill_type=skill.skill_type,
            interface_type=skill.interface_type,
            status="proposed",
            risk_level=skill.risk_level,
            manifest_path=self.proposed_service._relative_path(manifest_path),
            instructions_path=skill.instructions_path,
            input_schema_json=skill.input_schema_json,
            output_schema_json=skill.output_schema_json,
            tool_ui_schema_json=skill.tool_ui_schema_json,
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
            "description": plan.get("goal") or generation_request.user_message,
            "skill_type": plan["skill_type"],
            "interface_type": plan.get("interface_type", "chat"),
            "status": "building",
            "risk_level": plan["risk_level"],
            "manifest_path": self.proposed_service._relative_path(proposed_dir / "manifest.json"),
            "instructions_path": self._planned_instructions_path(plan),
            "input_schema_json": plan.get("input_schema"),
            "output_schema_json": plan.get("output_schema"),
            "tool_ui_schema_json": plan.get("tool_ui_schema"),
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
        if plan.get("skill_type") == "instruction":
            return "SKILL.md"
        files = plan.get("files_to_generate")
        if isinstance(files, list) and any(str(path).replace("\\", "/") == "SKILL.md" for path in files):
            return "SKILL.md"
        return None

    def _pm_build_time_summary(self, blueprint: dict[str, Any]) -> str:
        return (
            f"Build {blueprint.get('skill_name')} as a {blueprint.get('skill_type')} skill. "
            "ProductManager will define Builder-owned package files in the task DAG only after approval. "
            "Approval lets Codex generate proposed files only; it does not install or run the skill."
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
        plan["requested_permissions"] = runtime_permissions
        plan["requested_network_domains"] = runtime_permissions["network"]
        plan["requested_dependencies"] = runtime["dependencies"]
        generation_request.plan_json = plan
        generation_request.requested_permissions_json = runtime_permissions
        generation_request.requested_dependencies_json = runtime["dependencies"]
        generation_request.requested_network_domains_json = runtime_permissions["network"]
        self.db.commit()
        self.db.refresh(generation_request)
        return permission_plan

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
        raw_permission_plan = self._read_json_artifact(agent_run, "permissions.json")
        final_permission_plan = effective_permission_plan(
            raw_permission_plan,
            skill_type=str((agent_run.blueprint_json or {}).get("skill_type") or generation_request.proposed_skill_type),
        )
        permission_path = self._write_json_artifact(agent_run, "permissions.json", final_permission_plan)
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
        for step in agent_run.steps:
            if (
                step.step_name == "product_manager"
                and (step.input_json or {}).get("action") == "backend_build_time_permission_review"
            ):
                output = dict(step.output_json or {})
                output["permission_plan"] = final_permission_plan
                output["permission_path"] = permission_path
                step.output_json = output
        self.db.commit()
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

    def _selected_memory_facts(self) -> list[dict[str, Any]]:
        now = utc_now()
        facts = self.db.scalars(
            select(MemoryFact)
            .where(MemoryFact.user_editable.is_(True))
            .order_by(MemoryFact.updated_at.desc(), MemoryFact.id.desc())
            .limit(20)
        ).all()
        selected: list[dict[str, Any]] = []
        for fact in facts:
            if fact.expires_at is not None and fact.expires_at <= now:
                continue
            selected.append(
                {
                    "id": fact.id,
                    "key": fact.key,
                    "value": fact.value,
                    "category": fact.category,
                    "sensitivity": fact.sensitivity,
                }
            )
        return selected

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
            if step.status == "waiting_for_approval" and (step.output_json or {}).get("permission_request_id")
        ]
        for step in waiting_steps:
            step.status = "succeeded"
            step.ended_at = utc_now()
            output = dict(step.output_json or {})
            output["status"] = "approved"
            step.output_json = output
            step.logs = f"{step.logs or ''}\n\nApproved by local user.".strip()
        self.db.commit()

    def _milestone_by_name(self, agent_run: AgentRun, name: str) -> dict[str, Any]:
        return self._task_by_id(agent_run, name)

    def _next_task_node_id(self, agent_run: AgentRun, current_name: str) -> str | None:
        nodes = self._topological_task_nodes(self._task_dag(agent_run))
        for index, node in enumerate(nodes):
            if node["id"] == current_name:
                if index + 1 < len(nodes):
                    return nodes[index + 1]["id"]
                return None
        return None

    def _task_dag(self, agent_run: AgentRun) -> dict[str, Any]:
        payload = self._read_json_artifact(agent_run, "task_dag.json")
        if payload:
            return payload
        return {
            "schema_version": 1,
            "nodes": [
                {
                    "id": agent_run.current_task_id or DEFAULT_TASK_ID,
                    "title": "Compatibility task",
                    "summary": "Compatibility task node.",
                    "depends_on": [],
                    "difficulty": "medium",
                    "requires_tests": True,
                    "parallel_safe": True,
                    "expected_output_paths": ["manifest.json"],
                    "file_write_claims": ["manifest.json"],
                    "acceptance_criteria": ["validation passes"],
                    "test_expectations": ["tests pass"],
                    "interface_artifact_expectations": ["declare generated files"],
                }
            ],
            "final_e2e_expectations": [],
        }

    def _task_nodes(self, agent_run: AgentRun) -> list[dict[str, Any]]:
        return self._task_nodes_from_dag(self._task_dag(agent_run))

    def _task_nodes_from_dag(self, task_dag: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = task_dag.get("nodes")
        return [dict(node) for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []

    def _task_by_id(self, agent_run: AgentRun, task_id: str) -> dict[str, Any]:
        for node in self._task_nodes(agent_run):
            if node.get("id") == task_id:
                return node
        return {
            "id": task_id,
            "title": task_id.replace("_", " ").title(),
            "depends_on": [],
            "acceptance_criteria": [],
            "expected_output_paths": ["manifest.json"],
            "file_write_claims": ["manifest.json"],
            "requires_tests": True,
        }

    def _validate_task_dag(self, task_dag: dict[str, Any], blueprint: dict[str, Any]) -> None:
        nodes = self._task_nodes_from_dag(task_dag)
        if not nodes:
            raise AgentWorkflowError("Task DAG must contain at least one node")
        node_by_id: dict[str, dict[str, Any]] = {}
        for node in nodes:
            node_id = str(node.get("id") or "")
            if not self._is_safe_path_segment(node_id):
                raise AgentWorkflowError(f"Task node id is not a safe path segment: {node_id}")
            if node_id in node_by_id:
                raise AgentWorkflowError(f"Duplicate task node id: {node_id}")
            if not node.get("acceptance_criteria"):
                raise AgentWorkflowError(f"Task node {node_id} must include acceptance criteria")
            if not node.get("expected_output_paths"):
                raise AgentWorkflowError(f"Task node {node_id} must include expected output paths")
            for api_id in node.get("backend_api_ids", []) or []:
                try:
                    normalized_api_id = int(api_id)
                except (TypeError, ValueError):
                    raise AgentWorkflowError(f"Task node {node_id} references invalid backend API id: {api_id}") from None
                if normalized_api_id not in valid_backend_api_ids():
                    raise AgentWorkflowError(f"Task node {node_id} references unknown backend API id: {api_id}")
            node_by_id[node_id] = node
        for node in nodes:
            for dep in node.get("depends_on", []):
                if dep not in node_by_id:
                    raise AgentWorkflowError(f"Task node {node['id']} depends on missing node {dep}")
        self._topological_task_nodes(task_dag)
        if blueprint.get("skill_type") == "automation" and not any(node.get("requires_tests") for node in nodes):
            raise AgentWorkflowError("Automation skill DAG must include at least one tested node")
        for left in nodes:
            for right in nodes:
                if left["id"] >= right["id"]:
                    continue
                if self._has_dependency_path(task_dag, left["id"], right["id"]) or self._has_dependency_path(
                    task_dag, right["id"], left["id"]
                ):
                    continue
                overlap = set(left.get("file_write_claims", [])) & set(right.get("file_write_claims", []))
                if overlap:
                    raise AgentWorkflowError(
                        f"Task nodes {left['id']} and {right['id']} have overlapping file write claims without dependency ordering: {sorted(overlap)}"
                    )

    def _topological_task_nodes(self, task_dag: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = self._task_nodes_from_dag(task_dag)
        node_by_id = {node["id"]: node for node in nodes if "id" in node}
        visited: set[str] = set()
        visiting: set[str] = set()
        ordered: list[dict[str, Any]] = []

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise AgentWorkflowError("Task DAG is cyclic")
            if node_id not in node_by_id:
                raise AgentWorkflowError(f"Task DAG references missing node: {node_id}")
            visiting.add(node_id)
            for dep in node_by_id[node_id].get("depends_on", []):
                visit(str(dep))
            visiting.remove(node_id)
            visited.add(node_id)
            ordered.append(node_by_id[node_id])

        for node_id in node_by_id:
            visit(node_id)
        return ordered

    def _task_execution_batches(
        self,
        task_dag: dict[str, Any],
        *,
        completed_task_ids: set[str] | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Group simultaneously ready nodes so quota checks occur at safe batch boundaries.

        The current shared skill workspace still serializes each node inside a batch. Keeping
        ready nodes together ensures a future parallel executor can start the entire admitted
        batch, let all active nodes finish, and pause before admitting another batch.
        """
        nodes = self._task_nodes_from_dag(task_dag)
        remaining = {str(node["id"]): node for node in nodes if str(node["id"]) not in (completed_task_ids or set())}
        completed = set(completed_task_ids or set())
        batches: list[list[dict[str, Any]]] = []
        while remaining:
            ready = [
                node
                for node in nodes
                if str(node["id"]) in remaining
                and {str(dependency) for dependency in node.get("depends_on", [])}.issubset(completed)
            ]
            if not ready:
                raise AgentWorkflowError("Task DAG has no ready nodes; dependency state is invalid")
            parallel_ready = [node for node in ready if node.get("parallel_safe", False)]
            if parallel_ready:
                batches.append(parallel_ready)
            batches.extend([[node] for node in ready if not node.get("parallel_safe", False)])
            for node in ready:
                node_id = str(node["id"])
                completed.add(node_id)
                remaining.pop(node_id, None)
        return batches

    def _has_dependency_path(self, task_dag: dict[str, Any], start: str, target: str) -> bool:
        node_by_id = {node["id"]: node for node in self._task_nodes_from_dag(task_dag) if "id" in node}
        stack = list(node_by_id.get(target, {}).get("depends_on", []))
        while stack:
            node_id = str(stack.pop())
            if node_id == start:
                return True
            stack.extend(node_by_id.get(node_id, {}).get("depends_on", []))
        return False

    def _is_safe_path_segment(self, value: str) -> bool:
        return bool(value) and all(char.isalnum() or char in {"_", "-"} for char in value)

    def _failure_count(self, agent_run: AgentRun, task_node_id: str) -> int:
        return int((agent_run.failure_count_json or {}).get(task_node_id, 0))

    def _increment_failure_count(self, agent_run: AgentRun, task_node_id: str) -> None:
        counts = dict(agent_run.failure_count_json or {})
        counts[task_node_id] = int(counts.get(task_node_id, 0)) + 1
        agent_run.failure_count_json = counts
        self.db.commit()

    def _write_failure_log(self, agent_run: AgentRun, task_node_id: str, validation: Any) -> None:
        log_dir = self._artifact_dir(agent_run) / "tasks" / task_node_id
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
        return self._read_json_artifact(agent_run, "permissions.json")

    def _agent_blueprint(self, agent_run: AgentRun) -> dict[str, Any]:
        blueprint = dict(agent_run.blueprint_json or {})
        blueprint.pop("permission_plan", None)
        return blueprint

    def _builder_task_node(self, task_node: dict[str, Any]) -> dict[str, Any]:
        useful_fields = [
            "id",
            "summary",
            "expected_output_paths",
            "file_write_claims",
            "acceptance_criteria",
            "interface_artifact_expectations",
        ]
        return {key: task_node[key] for key in useful_fields if key in task_node}

    def _tester_task_node(self, task_node: dict[str, Any]) -> dict[str, Any]:
        useful_fields = [
            "id",
            "summary",
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
            "permission_bounds": self._agent_permission_bounds(self._permission_plan(agent_run)),
            "task_node": self._builder_task_node(task_node),
            "backend_api_context": backend_api_context(task_node.get("backend_api_ids", [])),
            "parent_interface_artifacts": self._direct_parent_interface_artifacts(agent_run, task_node),
            "workspace_paths": self._task_relevant_paths(task_node),
        }
        return context

    def _agent_permission_bounds(self, permission_plan: dict[str, Any]) -> dict[str, Any]:
        build_time = permission_plan.get("build_time") if isinstance(permission_plan.get("build_time"), dict) else {}
        runtime = permission_plan.get("runtime") if isinstance(permission_plan.get("runtime"), dict) else {}
        return {
            "build_time": dict(build_time),
            "runtime": dict(runtime),
            "blocked_capabilities": default_banned_permissions(),
        }

    def _final_blueprint_contract(self, agent_run: AgentRun) -> dict[str, Any]:
        blueprint = self._agent_blueprint(agent_run)
        fields = ("goal", "expected_behavior", "schedule", "acceptance_criteria")
        return {field: blueprint[field] for field in fields if field in blueprint}

    def _compact_interface_artifacts(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fields = ("task_id", "interfaces", "contracts_for_children", "known_limitations")
        return [{field: artifact[field] for field in fields if field in artifact} for artifact in artifacts]

    def _task_interface_artifact(self, agent_run: AgentRun, task_id: str) -> dict[str, Any]:
        path = self._artifact_dir(agent_run) / "tasks" / task_id / "interface_artifact.json"
        if not path.is_file():
            return {}
        return {"task_id": task_id, **json.loads(path.read_text(encoding="utf-8"))}

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
        for key in ("expected_output_paths", "file_write_claims"):
            for item in task_node.get(key, []) or []:
                value = str(item)
                if value and value not in paths and not value.startswith("tests/"):
                    paths.append(value)
        return paths

    def _final_e2e_expectations(self, agent_run: AgentRun) -> list[str]:
        expectations = self._task_dag(agent_run).get("final_e2e_expectations", [])
        return [str(item) for item in expectations] if isinstance(expectations, list) else []

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
        excluded_parts = {".agents", ".deps", ".git", ".pytest_cache", "__pycache__"}
        return [
            path.relative_to(skill_dir).as_posix()
            for path in sorted(skill_dir.rglob("*"))
            if path.is_file()
            and path.name not in excluded_names
            and not any(part in excluded_parts for part in path.parts)
        ]

    def _version_file_snapshot(self, version: Any) -> dict[str, str]:
        version_dir = (self.project_root / version.folder_path).resolve()
        snapshot: dict[str, str] = {}
        for relative_path in ("manifest.json", "README.md", "SKILL.md", "skill.py"):
            path = version_dir / relative_path
            if path.is_file():
                snapshot[relative_path] = path.read_text(encoding="utf-8")[:12000]
        return snapshot

    def _version_test_paths(self, version: Any) -> list[str]:
        version_dir = (self.project_root / version.folder_path).resolve()
        tests_dir = version_dir / "tests"
        if not tests_dir.is_dir():
            return []
        return [path.relative_to(version_dir).as_posix() for path in sorted(tests_dir.rglob("test_*.py"))]

    def _artifact_dir(self, agent_run: AgentRun) -> Path:
        path = self.project_root / "runtime" / "agent_runs" / f"run_{agent_run.id}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _initialize_artifact_dir(self, agent_run: AgentRun) -> Path:
        path = self.project_root / "runtime" / "agent_runs" / f"run_{agent_run.id}"
        if path.exists() and any(path.iterdir()):
            orphaned_dir = path.parent / "orphaned"
            orphaned_dir.mkdir(parents=True, exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
            path.replace(orphaned_dir / f"{path.name}_{timestamp}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _artifact_relative_path(self, agent_run: AgentRun, filename: str) -> str:
        return (self._artifact_dir(agent_run) / filename).resolve().relative_to(self.project_root).as_posix()

    def _write_task_artifacts(self, agent_run: AgentRun, task_dag: dict[str, Any]) -> list[str]:
        task_root = self._artifact_dir(agent_run) / "tasks"
        task_root.mkdir(parents=True, exist_ok=True)
        paths = []
        nodes = self._task_nodes_from_dag(task_dag)
        for index, node in enumerate(nodes, start=1):
            payload = {**node, "index": index, "status": "pending"}
            path = task_root / f"{node['id']}.json"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            paths.append(path.resolve().relative_to(self.project_root).as_posix())
            (task_root / node["id"]).mkdir(exist_ok=True)
        expected = {f"{node['id']}.json" for node in nodes}
        for existing in task_root.glob("*.json"):
            if existing.name not in expected:
                existing.unlink()
        return paths

    def _write_task_statuses(self, agent_run: AgentRun, statuses: dict[str, str]) -> None:
        final_summary = dict(agent_run.final_summary_json or {})
        final_summary["task_statuses"] = statuses
        agent_run.final_summary_json = final_summary
        for task_id, status in statuses.items():
            path = self._artifact_dir(agent_run) / "tasks" / f"{task_id}.json"
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = status
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.db.commit()

    def _write_task_test_result(self, agent_run: AgentRun, task_id: str, validation: Any) -> str:
        path = self._artifact_dir(agent_run) / "tasks" / task_id / "test_result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = validation.model_dump(mode="json") if hasattr(validation, "model_dump") else {}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path.resolve().relative_to(self.project_root).as_posix()

    def _move_validated_interface_artifact(
        self,
        agent_run: AgentRun,
        skill: Skill,
        task_node: dict[str, Any],
    ) -> str:
        task_id = str(task_node.get("id") or agent_run.current_task_id or DEFAULT_TASK_ID)
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        source = skill_dir / "interface_artifact.json"
        if not source.is_file():
            raise AgentWorkflowError(f"Builder did not write interface_artifact.json for task {task_id}")
        try:
            raw_artifact = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentWorkflowError(f"Builder interface_artifact.json is not valid JSON: {exc}") from exc
        try:
            artifact = InterfaceArtifact.model_validate(raw_artifact)
        except ValidationError as exc:
            raise AgentWorkflowError(f"Builder interface_artifact.json is invalid: {exc}") from exc

        self._validate_interface_artifact(skill_dir, artifact, task_id, task_node, agent_run)
        destination = self._artifact_dir(agent_run) / "tasks" / task_id / "interface_artifact.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        return destination.resolve().relative_to(self.project_root).as_posix()

    def _parent_declared_paths(self, agent_run: AgentRun, task_node: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for artifact in self._parent_interface_artifacts(agent_run, task_node):
            for key in ("created_paths", "updated_paths"):
                values = artifact.get(key)
                if isinstance(values, list):
                    paths.update(str(value) for value in values)
        return paths

    def _validate_interface_artifact(
        self,
        skill_dir: Path,
        artifact: InterfaceArtifact,
        task_id: str,
        task_node: dict[str, Any],
        agent_run: AgentRun,
    ) -> None:
        declared_paths = set(artifact.created_paths) | set(artifact.updated_paths)
        expected_paths = {
            str(path).replace("\\", "/").removeprefix("./")
            for path in task_node.get("expected_output_paths", []) or []
        }
        missing_declarations = expected_paths - declared_paths
        if missing_declarations:
            raise AgentWorkflowError(
                f"Interface artifact does not declare expected task outputs: {sorted(missing_declarations)}"
            )
        allowed_paths = {
            str(path).replace("\\", "/").removeprefix("./")
            for path in task_node.get("file_write_claims", []) or []
        }
        allowed_paths.add("manifest.json")
        unexpected_paths = declared_paths - allowed_paths
        if unexpected_paths:
            raise AgentWorkflowError(
                f"Interface artifact declares paths outside task file_write_claims: {sorted(unexpected_paths)}"
            )

        parent_paths = self._parent_declared_paths(agent_run, task_node)
        update_contract_paths = parent_paths | {"manifest.json"}
        incorrectly_created = set(artifact.created_paths) & update_contract_paths
        incorrectly_updated = set(artifact.updated_paths) - update_contract_paths
        if incorrectly_created:
            raise AgentWorkflowError(
                f"Interface artifact marks existing contract paths as created: {sorted(incorrectly_created)}"
            )
        if incorrectly_updated:
            raise AgentWorkflowError(
                f"Interface artifact marks new task paths as updated: {sorted(incorrectly_updated)}"
            )

        skill_root = skill_dir.resolve()
        for relative_path in declared_paths:
            resolved = (skill_dir / relative_path).resolve()
            if not resolved.is_relative_to(skill_root) or not resolved.is_file():
                raise AgentWorkflowError(f"Interface artifact declares a missing or unsafe skill file: {relative_path}")

    def _parent_interface_artifacts(self, agent_run: AgentRun, task_node: dict[str, Any]) -> list[dict[str, Any]]:
        node_by_id = {str(node.get("id")): node for node in self._task_nodes(agent_run)}
        ordered_parent_ids: list[str] = []
        visited: set[str] = set()

        def collect(parent_id: str) -> None:
            if parent_id in visited:
                return
            visited.add(parent_id)
            parent = node_by_id.get(parent_id, {})
            for ancestor_id in parent.get("depends_on", []) or []:
                collect(str(ancestor_id))
            ordered_parent_ids.append(parent_id)

        for parent_id in task_node.get("depends_on", []) or []:
            collect(str(parent_id))

        artifacts = []
        for parent_id in ordered_parent_ids:
            path = self._artifact_dir(agent_run) / "tasks" / str(parent_id) / "interface_artifact.json"
            if path.is_file():
                artifacts.append({"task_id": str(parent_id), **json.loads(path.read_text(encoding="utf-8"))})
        return artifacts

    def _direct_parent_interface_artifacts(
        self,
        agent_run: AgentRun,
        task_node: dict[str, Any],
    ) -> list[dict[str, Any]]:
        artifacts = []
        for parent_id in task_node.get("depends_on", []) or []:
            path = self._artifact_dir(agent_run) / "tasks" / str(parent_id) / "interface_artifact.json"
            if path.is_file():
                artifacts.append({"task_id": str(parent_id), **json.loads(path.read_text(encoding="utf-8"))})
        return artifacts

    def _all_interface_artifacts(self, agent_run: AgentRun) -> list[dict[str, Any]]:
        artifacts = []
        for node in self._task_nodes(agent_run):
            path = self._artifact_dir(agent_run) / "tasks" / node["id"] / "interface_artifact.json"
            if path.is_file():
                artifacts.append({"task_id": str(node["id"]), **json.loads(path.read_text(encoding="utf-8"))})
        return artifacts

    def _write_json_artifact(self, agent_run: AgentRun, filename: str, payload: dict[str, Any]) -> str:
        path = self._artifact_dir(agent_run) / filename
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path.resolve().relative_to(self.project_root).as_posix()

    def _read_json_artifact(self, agent_run: AgentRun, filename: str) -> dict[str, Any]:
        path = self._artifact_dir(agent_run) / filename
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_text_artifact(self, agent_run: AgentRun, filename: str, content: str) -> str:
        path = self._artifact_dir(agent_run) / filename
        path.write_text(content, encoding="utf-8")
        return path.resolve().relative_to(self.project_root).as_posix()

    def _start_step(
        self,
        agent_run: AgentRun,
        step_name: str,
        *,
        task_node_id: str | None = None,
        input_json: dict[str, Any] | None = None,
        logs: str | None = None,
    ) -> AgentRunStep:
        self._ensure_not_cancelled(agent_run)
        step = AgentRunStep(
            agent_run_id=agent_run.id,
            step_name=step_name,
            task_node_id=task_node_id,
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
