import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentRunStep, Skill, SkillGenerationRequest
from app.services.codex_service import CodexGenerationError, CodexService
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService


class AgentWorkflowError(ValueError):
    pass


MAX_MILESTONE_FAILURES = 3
DEFAULT_MILESTONE = "initial_skill"


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
        if existing and existing.status in {"pending", "running", "waiting_for_approval", "succeeded"}:
            return existing

        blueprint = self._blueprint_from_generation_request(generation_request)
        building_skill = self._create_or_update_building_skill(generation_request, blueprint)
        generation_request.proposed_skill_id = building_skill.id
        agent_run = AgentRun(
            run_type="build_skill",
            status="running",
            skill_id=building_skill.id,
            generation_request_id=generation_request.id,
            user_request=generation_request.user_message,
            current_milestone=DEFAULT_MILESTONE,
            current_step="product_manager",
            failure_count_json={DEFAULT_MILESTONE: 0},
            blueprint_json=blueprint,
            summary="ProductManager created the skill blueprint.",
        )
        self.db.add(agent_run)
        self.db.commit()
        self.db.refresh(agent_run)

        pm_summary = self._pm_build_time_summary(blueprint)
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                milestone_name=DEFAULT_MILESTONE,
                input_json={"user_request": generation_request.user_message},
                logs="ProductManager created a concise blueprint and selected the next action.",
            ),
            "succeeded",
            output_json={
                "blueprint_json": blueprint,
                "decision_json": {
                    "decision": "request_permission",
                    "reason": "Build-time approval is required before Codex writes proposed files.",
                },
                "user_summary": pm_summary,
            },
            logs=pm_summary,
        )

        permission_request = PermissionService(self.db, project_root=self.project_root).create_build_time_request(
            generation_request
        )
        security_summary = self._security_build_time_summary(generation_request.plan_json, permission_request)
        self._apply_combined_permission_summary(permission_request, pm_summary, security_summary)
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "security_reviewer",
                milestone_name=DEFAULT_MILESTONE,
                input_json={"generation_request_id": generation_request.id},
                logs="SecurityReviewer analyzed build-time permissions and created the approval request.",
            ),
            "waiting_for_approval",
            output_json={
                "permission_request_id": permission_request.id,
                "status": permission_request.status,
                "risk_level": permission_request.risk_level,
                "product_manager_summary": pm_summary,
                "security_reviewer_summary": security_summary,
            },
            logs=f"ProductManager: {pm_summary}\n\nSecurityReviewer: {security_summary}",
        )
        agent_run.status = "waiting_for_approval"
        agent_run.current_step = "security_reviewer"
        agent_run.summary = "Waiting for one build-time approval."
        self.db.commit()
        self.db.refresh(agent_run)
        return agent_run

    def continue_build_after_approval(self, generation_request: SkillGenerationRequest) -> tuple[AgentRun, Skill, object]:
        agent_run = self.latest_run_for_generation(generation_request.id) or self.create_build_run(generation_request)
        self._ensure_not_cancelled(agent_run)
        decision = PermissionService(self.db, project_root=self.project_root).can_generate(generation_request)
        if not decision.allowed:
            agent_run.status = "waiting_for_approval"
            agent_run.current_step = "security_reviewer"
            self.db.commit()
            raise AgentWorkflowError(decision.reason)

        self._mark_waiting_security_step_approved(agent_run)
        agent_run.status = "running"
        agent_run.current_step = "builder"
        self.db.commit()

        try:
            builder_step = self._start_step(
                agent_run,
                "builder",
                milestone_name=DEFAULT_MILESTONE,
                input_json={
                    "mode": "build",
                    "generation_request_id": generation_request.id,
                    "milestone": self._current_milestone(agent_run),
                },
                logs="Builder is implementing the current milestone only.",
            )
            skill, validation = self.codex_service.generate_from_request(generation_request)
            self._finish_step(
                agent_run,
                builder_step,
                "succeeded",
                output_json={"skill_id": skill.id, "skill_name": skill.name, "mode": "build"},
                logs="Builder generated proposed skill files. The skill was not installed or run.",
            )

            validation = self._test_milestone(agent_run, skill, validation)
            while not validation.ok:
                self._increment_failure_count(agent_run, DEFAULT_MILESTONE)
                if self._failure_count(agent_run, DEFAULT_MILESTONE) > MAX_MILESTONE_FAILURES:
                    self._product_manager_stop_failed(agent_run, skill, validation)
                    return agent_run, skill, validation
                validation = self._repair_current_milestone(agent_run, skill, validation)

            self._product_manager_after_tests(agent_run, skill, validation)
            runtime_status = self._runtime_security_review(agent_run, skill, validation)
            self._product_manager_finish(agent_run, skill, validation, runtime_status)
            self.db.refresh(agent_run)
            return agent_run, skill, validation
        except Exception as exc:
            if agent_run.status != "blocked":
                self._fail_run(agent_run, str(exc))
            raise

    def create_repair_run(self, skill: Skill, user_request: str | None = None) -> AgentRun:
        if skill.status == "deleted":
            raise AgentWorkflowError("Deleted skills cannot be repaired")

        blueprint = self._blueprint_for_repair(skill, user_request)
        agent_run = AgentRun(
            run_type="repair_skill",
            status="running",
            skill_id=skill.id,
            user_request=user_request or f"Repair skill {skill.name}.",
            current_milestone="repair_skill",
            current_step="product_manager",
            failure_count_json={"repair_skill": 0},
            blueprint_json=blueprint,
            summary="ProductManager created a repair blueprint.",
        )
        self.db.add(agent_run)
        self.db.commit()
        self.db.refresh(agent_run)

        try:
            repair_skill = self._prepare_repair_target(skill, agent_run)
            pm_summary = f"Repair proposed skill {repair_skill.name} using existing failure context and tests."
            self._finish_step(
                agent_run,
                self._start_step(
                    agent_run,
                    "product_manager",
                    milestone_name="repair_skill",
                    input_json={"skill_id": skill.id},
                    logs="ProductManager reviewed the repair request and selected repair mode.",
                ),
                "succeeded",
                output_json={
                    "blueprint_json": blueprint,
                    "decision_json": {"decision": "repair_current_milestone"},
                    "user_summary": pm_summary,
                },
                logs=pm_summary,
            )

            validation = self._repair_current_milestone(agent_run, repair_skill, None, milestone_name="repair_skill")
            while not validation.ok:
                self._increment_failure_count(agent_run, "repair_skill")
                if self._failure_count(agent_run, "repair_skill") > MAX_MILESTONE_FAILURES:
                    self._product_manager_stop_failed(agent_run, repair_skill, validation)
                    return agent_run
                validation = self._repair_current_milestone(agent_run, repair_skill, validation, milestone_name="repair_skill")

            self._product_manager_after_tests(agent_run, repair_skill, validation, milestone_name="repair_skill")
            runtime_status = self._runtime_security_review(agent_run, repair_skill, validation, milestone_name="repair_skill")
            self._product_manager_finish(agent_run, repair_skill, validation, runtime_status, milestone_name="repair_skill")
        except Exception as exc:
            if agent_run.status != "blocked":
                self._fail_run(agent_run, str(exc))
            raise
        return agent_run

    def resume_run(self, agent_run: AgentRun) -> AgentRun:
        self._ensure_not_cancelled(agent_run)
        if agent_run.status not in {"waiting_for_approval", "pending"}:
            return agent_run
        if agent_run.generation_request_id:
            generation_request = self.db.get(SkillGenerationRequest, agent_run.generation_request_id)
            if generation_request is None:
                raise AgentWorkflowError("Generation request no longer exists")
            self.continue_build_after_approval(generation_request)
            self.db.refresh(agent_run)
            return agent_run
        return agent_run

    def retry_current_milestone(self, agent_run: AgentRun) -> AgentRun:
        self._ensure_not_cancelled(agent_run)
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
        raise AgentWorkflowError("Retry current milestone is not available for this run")

    def retry_step(self, agent_run: AgentRun, step: AgentRunStep) -> AgentRun:
        if step.agent_run_id != agent_run.id:
            raise AgentWorkflowError("Step does not belong to this agent run")
        return self.retry_current_milestone(agent_run)

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

    def _test_milestone(self, agent_run: AgentRun, skill: Skill, validation: Any, milestone_name: str = DEFAULT_MILESTONE) -> Any:
        tester_step = self._start_step(
            agent_run,
            "tester",
            milestone_name=milestone_name,
            input_json={
                "skill_id": skill.id,
                "blueprint_json": agent_run.blueprint_json,
                "milestone": self._milestone_by_name(agent_run, milestone_name),
            },
            logs="Tester validates manifest schema, tests, and JSON stdin/stdout expectations through the existing safe path.",
        )
        self._finish_step(
            agent_run,
            tester_step,
            "succeeded" if validation.ok else "failed",
            output_json={
                "test_result_json": validation.model_dump(mode="json"),
                "failure_log": validation.error_message or validation.stderr or "",
            },
            logs="Tester completed validation. No skill task was run automatically.",
            error_message=validation.error_message,
        )
        if not validation.ok:
            self._write_failure_log(agent_run, milestone_name, validation)
        return validation

    def _repair_current_milestone(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any | None,
        milestone_name: str = DEFAULT_MILESTONE,
    ) -> Any:
        context = self._repair_context(skill, agent_run)
        if validation is not None:
            context["test_result_json"] = validation.model_dump(mode="json")
        builder_step = self._start_step(
            agent_run,
            "builder",
            milestone_name=milestone_name,
            input_json={
                "mode": "repair",
                "skill_id": skill.id,
                "milestone": self._milestone_by_name(agent_run, milestone_name),
                "failure_context": context,
            },
            logs="Builder is repairing the current milestone using Tester failure output.",
        )
        try:
            result = self.codex_service.repair_skill(skill, context)
        except CodexGenerationError as exc:
            if "USER_ACTION_REQUIRED:" in str(exc):
                self._builder_user_action_required(agent_run, builder_step, str(exc), milestone_name)
                raise AgentWorkflowError(str(exc)) from exc
            raise
        self._finish_step(
            agent_run,
            builder_step,
            "succeeded",
            output_json={"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode, "mode": "repair"},
            logs="Builder proposed a repair. The skill was not installed or run.",
        )
        repaired_validation = self.proposed_service.validate_proposed_skill(skill)
        return self._test_milestone(agent_run, skill, repaired_validation, milestone_name=milestone_name)

    def _product_manager_after_tests(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any,
        milestone_name: str = DEFAULT_MILESTONE,
    ) -> None:
        decision = "finish_ready_for_review" if self._all_milestones_complete(agent_run) else "build_next_milestone"
        summary = (
            f"Milestone {milestone_name} passed. "
            "ProductManager considers the planned blueprint complete and is sending it to SecurityReviewer."
        )
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                milestone_name=milestone_name,
                input_json={"skill_id": skill.id, "test_result_json": validation.model_dump(mode="json")},
                logs="ProductManager reviewed the passing milestone result.",
            ),
            "succeeded",
            output_json={"decision_json": {"decision": decision}, "user_summary": summary},
            logs=summary,
        )

    def _runtime_security_review(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any,
        milestone_name: str = DEFAULT_MILESTONE,
    ) -> str:
        runtime_request = PermissionService(self.db, project_root=self.project_root).create_runtime_request(skill)
        status = "waiting_for_approval" if runtime_request.status == "pending" else "succeeded"
        security_summary = self._security_runtime_summary(skill, runtime_request)
        pm_summary = self._pm_runtime_summary(skill, validation)
        self._apply_combined_permission_summary(runtime_request, pm_summary, security_summary)
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "security_reviewer",
                milestone_name=milestone_name,
                input_json={"skill_id": skill.id},
                logs="SecurityReviewer analyzed actual manifest runtime permissions.",
            ),
            status,
            output_json={
                "security_review_json": {
                    "permission_request_id": runtime_request.id,
                    "status": runtime_request.status,
                    "risk_level": runtime_request.risk_level,
                    "permission_expansion": runtime_request.reason_json.get("permission_expansion", {}),
                    "runner_unsupported": runtime_request.reason_json.get("runner_unsupported", []),
                },
                "product_manager_summary": pm_summary,
                "security_reviewer_summary": security_summary,
            },
            logs=f"ProductManager: {pm_summary}\n\nSecurityReviewer: {security_summary}",
        )
        return runtime_request.status

    def _product_manager_finish(
        self,
        agent_run: AgentRun,
        skill: Skill,
        validation: Any,
        runtime_status: str,
        milestone_name: str = DEFAULT_MILESTONE,
    ) -> None:
        summary = (
            f"Skill {skill.name} is ready for user review. Validation passed; runtime permission request is {runtime_status}. "
            "The skill remains proposed and was not installed or run automatically."
        )
        final_summary = {
            "skill_id": skill.id,
            "skill_name": skill.name,
            "validation_ok": validation.ok,
            "runtime_permission_status": runtime_status,
            "user_summary": summary,
        }
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                milestone_name=milestone_name,
                input_json={"skill_id": skill.id},
                logs="ProductManager wrote the completion summary.",
            ),
            "succeeded",
            output_json={"decision_json": {"decision": "finish_ready_for_review"}, "user_summary": summary},
            logs=summary,
        )
        agent_run.skill_id = skill.id
        agent_run.status = "succeeded"
        agent_run.current_step = "product_manager"
        agent_run.summary = summary
        agent_run.final_summary_json = final_summary
        agent_run.completed_at = utc_now()
        agent_run.error_message = None
        self.db.commit()
        self.db.refresh(agent_run)

    def _product_manager_stop_failed(self, agent_run: AgentRun, skill: Skill, validation: Any) -> None:
        milestone_name = agent_run.current_milestone or DEFAULT_MILESTONE
        summary = (
            f"Milestone {milestone_name} failed more than {MAX_MILESTONE_FAILURES} times. "
            f"Latest failure: {validation.error_message or 'tests failed'}. User review is recommended."
        )
        self._finish_step(
            agent_run,
            self._start_step(
                agent_run,
                "product_manager",
                milestone_name=milestone_name,
                input_json={"skill_id": skill.id, "failure_count_json": agent_run.failure_count_json},
                logs="ProductManager stopped the workflow after repeated failures.",
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
        agent_run.final_summary_json = {"user_summary": summary, "failure_count_json": agent_run.failure_count_json}
        self.db.commit()

    def _builder_user_action_required(
        self,
        agent_run: AgentRun,
        builder_step: AgentRunStep,
        message: str,
        milestone_name: str,
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
        agent_run.current_milestone = milestone_name
        agent_run.current_step = "builder"
        agent_run.summary = report["exact_blocker"]
        agent_run.final_summary_json = {"user_action_required": report}
        agent_run.completed_at = utc_now()
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

    def _blueprint_from_generation_request(self, generation_request: SkillGenerationRequest) -> dict[str, Any]:
        plan = generation_request.plan_json
        acceptance_criteria = [
            "manifest.json is valid",
            "required skill files exist",
            "automation or hybrid tests pass",
            "executable skills use JSON stdin/stdout",
        ]
        if plan.get("interface_type") == "tool":
            acceptance_criteria.append("tool_ui_schema is present so the Tools page can render a user-friendly UI")
        return {
            "goal": plan.get("goal") or generation_request.user_message,
            "skill_name": plan.get("skill_name"),
            "skill_type": plan.get("skill_type"),
            "interface_type": plan.get("interface_type", "chat"),
            "expected_files": plan.get("files_to_generate", []),
            "expected_behavior": plan.get("expected_output", {}),
            "milestones": [
                {
                    "name": DEFAULT_MILESTONE,
                    "summary": "Create the proposed skill package and tests.",
                    "acceptance_criteria": acceptance_criteria,
                }
            ],
        }

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
            "instructions_path": "SKILL.md" if plan["skill_type"] in {"instruction", "hybrid"} else None,
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

    def _blueprint_for_repair(self, skill: Skill, user_request: str | None) -> dict[str, Any]:
        return {
            "goal": user_request or f"Repair {skill.name}.",
            "skill_name": skill.name,
            "skill_type": skill.skill_type,
            "interface_type": skill.interface_type,
            "expected_files": ["manifest.json", "README.md"],
            "milestones": [
                {
                    "name": "repair_skill",
                    "summary": "Repair the proposed skill package and confirm tests pass.",
                    "acceptance_criteria": ["manifest.json is valid", "tests pass", "permissions do not expand silently"],
                }
            ],
        }

    def _pm_build_time_summary(self, blueprint: dict[str, Any]) -> str:
        files = ", ".join(blueprint.get("expected_files", [])) or "skill files"
        return (
            f"Build {blueprint.get('skill_name')} as a {blueprint.get('skill_type')} skill. "
            f"Milestone: {blueprint['milestones'][0]['summary']} Expected files: {files}. "
            "Approval lets Codex generate proposed files only; it does not install or run the skill."
        )

    def _security_build_time_summary(self, plan: dict[str, Any], permission_request: Any) -> str:
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

    def _security_runtime_summary(self, skill: Skill, runtime_request: Any) -> str:
        return (
            f"Actual manifest runtime risk is {runtime_request.risk_level}. "
            f"Permissions: {runtime_request.requested_permissions_json}. "
            f"Unsupported runner items: {runtime_request.reason_json.get('runner_unsupported', [])}. "
            f"Permission expansion: {runtime_request.reason_json.get('permission_expansion', {})}."
        )

    def _apply_combined_permission_summary(self, permission_request: Any, pm_summary: str, security_summary: str) -> None:
        permission_request.reason_json = {
            **(permission_request.reason_json or {}),
            "product_manager_summary": pm_summary,
            "security_reviewer_summary": security_summary,
        }
        permission_request.user_explanation = f"ProductManager: {pm_summary}\n\nSecurityReviewer: {security_summary}"
        permission_request.reason = permission_request.user_explanation
        self.db.commit()
        self.db.refresh(permission_request)

    def _mark_waiting_security_step_approved(self, agent_run: AgentRun) -> None:
        waiting_steps = [
            step
            for step in agent_run.steps
            if step.step_name == "security_reviewer" and step.status == "waiting_for_approval"
        ]
        for step in waiting_steps:
            step.status = "succeeded"
            step.ended_at = utc_now()
            output = dict(step.output_json or {})
            output["status"] = "approved"
            step.output_json = output
            step.logs = f"{step.logs or ''}\n\nApproved by local user.".strip()
        self.db.commit()

    def _repair_context(self, skill: Skill, agent_run: AgentRun) -> dict[str, Any]:
        runs = []
        if skill.runs:
            for run in sorted(skill.runs, key=lambda item: item.id, reverse=True)[:3]:
                runs.append(
                    {
                        "status": run.status,
                        "input_json": run.input_json,
                        "output_json": run.output_json,
                        "stdout": run.stdout,
                        "stderr": run.stderr,
                        "error_message": run.error_message,
                    }
                )
        return {
            "user_request": agent_run.user_request,
            "skill_id": skill.id,
            "skill_name": skill.name,
            "status": skill.status,
            "blueprint_json": agent_run.blueprint_json,
            "recent_runs": runs,
        }

    def _current_milestone(self, agent_run: AgentRun) -> dict[str, Any]:
        return self._milestone_by_name(agent_run, agent_run.current_milestone or DEFAULT_MILESTONE)

    def _milestone_by_name(self, agent_run: AgentRun, name: str) -> dict[str, Any]:
        for milestone in (agent_run.blueprint_json or {}).get("milestones", []):
            if milestone.get("name") == name:
                return milestone
        return {"name": name, "acceptance_criteria": []}

    def _all_milestones_complete(self, agent_run: AgentRun) -> bool:
        return True

    def _failure_count(self, agent_run: AgentRun, milestone_name: str) -> int:
        return int((agent_run.failure_count_json or {}).get(milestone_name, 0))

    def _increment_failure_count(self, agent_run: AgentRun, milestone_name: str) -> None:
        counts = dict(agent_run.failure_count_json or {})
        counts[milestone_name] = int(counts.get(milestone_name, 0)) + 1
        agent_run.failure_count_json = counts
        self.db.commit()

    def _write_failure_log(self, agent_run: AgentRun, milestone_name: str, validation: Any) -> None:
        log_dir = self.project_root / "runtime" / "agent_runs" / f"run_{agent_run.id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{milestone_name}_failure.log"
        content = "\n".join(
            [
                f"milestone={milestone_name}",
                f"error={validation.error_message or ''}",
                "stdout:",
                validation.stdout or "",
                "stderr:",
                validation.stderr or "",
            ]
        )
        log_path.write_text(content, encoding="utf-8")

    def _start_step(
        self,
        agent_run: AgentRun,
        step_name: str,
        *,
        milestone_name: str | None = None,
        input_json: dict[str, Any] | None = None,
        logs: str | None = None,
    ) -> AgentRunStep:
        self._ensure_not_cancelled(agent_run)
        step = AgentRunStep(
            agent_run_id=agent_run.id,
            step_name=step_name,
            milestone_name=milestone_name,
            status="running",
            input_json=input_json,
            logs=logs,
            started_at=utc_now(),
        )
        self.db.add(step)
        agent_run.status = "running"
        agent_run.current_step = step_name
        agent_run.current_milestone = milestone_name or agent_run.current_milestone
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
        self.db.commit()

    def _ensure_not_cancelled(self, agent_run: AgentRun) -> None:
        if agent_run.status == "cancelled":
            raise AgentWorkflowError("Agent run is cancelled")
