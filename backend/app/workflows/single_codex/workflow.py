from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.models import Skill, SkillGenerationRequest
from app.workflows.base import SINGLE_CODEX_WORKFLOW, ProjectBuildWorkflowError
from app.workflows.single_codex.prompts import build_prompt

if TYPE_CHECKING:
    from app.models import AgentRun
    from app.services.agent_workflow_service import AgentWorkflowService


class SingleCodexBuildWorkflow:
    name = SINGLE_CODEX_WORKFLOW

    def execute(
        self,
        service: AgentWorkflowService,
        generation_request: SkillGenerationRequest,
        agent_run: AgentRun,
        permission_plan: dict[str, Any],
    ) -> tuple[AgentRun, Skill, None]:
        existing_skill = service.db.get(Skill, generation_request.proposed_skill_id)
        if existing_skill is None:
            raise ProjectBuildWorkflowError("Single-Codex build no longer has a proposed skill record")
        if service._pause_if_usage_below_reserve(agent_run):
            return agent_run, existing_skill, None

        agent_run.status = "running"
        agent_run.current_step = "builder"
        agent_run.current_milestone = "single_codex"
        agent_run.summary = "Codex is planning, building, and testing the skill in one invocation."
        service.db.commit()
        step = service._start_step(
            agent_run,
            "builder",
            milestone_name="single_codex",
            input_json={
                "action": "single_codex_build",
                "blueprint_json": agent_run.blueprint_json,
                "permission_plan": permission_plan,
            },
            logs="Codex is handling planning, implementation, and testing in one controlled skill workspace.",
        )
        skill, result = service.codex_service.run_single_codex_build(
            generation_request,
            agent_run.blueprint_json or {},
            permission_plan,
            prompt_builder=build_prompt,
        )
        service._finish_step(
            agent_run,
            step,
            "succeeded",
            output_json={
                "skill_id": skill.id,
                "skill_name": skill.name,
                "action": "single_codex_build",
                **service._subprocess_output(result),
                "exit_code": result.returncode,
            },
            logs="Codex completed the single-invocation build and test workflow. The skill was not installed or run.",
        )

        summary = (
            f"Codex completed planning, building, and testing {skill.name} in one invocation. "
            "The skill remains proposed and was not installed or run automatically."
        )
        runtime_status = service._runtime_permission_review(
            agent_run,
            skill,
            None,
            milestone_name="single_codex",
            pm_summary=summary,
        )
        agent_run.skill_id = skill.id
        agent_run.status = "succeeded"
        agent_run.current_step = "builder"
        agent_run.summary = summary
        agent_run.final_summary_json = {
            "skill_id": skill.id,
            "skill_name": skill.name,
            "runtime_permission_status": runtime_status,
            "backend_final_validation": "pending_todo",
            "user_summary": summary,
        }
        agent_run.completed_at = datetime.now(UTC)
        agent_run.error_message = None
        skill.status = "proposed"
        generation_request.status = "generated"
        generation_request.proposed_skill_id = skill.id
        generation_request.error_message = None
        service.db.commit()
        service.db.refresh(agent_run)
        return agent_run, skill, None

    def resume(self, service: AgentWorkflowService, agent_run: AgentRun) -> AgentRun:
        generation_request = service.db.get(SkillGenerationRequest, agent_run.generation_request_id)
        if generation_request is None:
            raise ProjectBuildWorkflowError("Single-Codex build generation request is unavailable")
        generation_request.status = "approved"
        agent_run.pause_reason = None
        agent_run.error_message = None
        service.db.commit()
        permission_plan = service._read_json_artifact(agent_run, "permissions.json")
        self.execute(service, generation_request, agent_run, permission_plan)
        return agent_run

    def retry_failed(self, service: AgentWorkflowService, agent_run: AgentRun) -> AgentRun:
        return self.resume(service, agent_run)
