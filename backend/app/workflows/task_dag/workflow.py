from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models import Skill, SkillGenerationRequest
from app.services.backend_api_catalog import backend_api_index, backend_api_index_file
from app.services.codex_service import CodexGenerationError
from app.services.integration_registry import operation_index
from app.workflows.base import (
    FINAL_E2E_FAILURE_KEY,
    MAX_TASK_FAILURES,
    TASK_DAG_WORKFLOW,
    ProjectBuildWorkflowError,
)
from app.workflows.task_dag.prompts import build_product_manager_prompt

if TYPE_CHECKING:
    from app.models import AgentRun
    from app.services.agent_workflow_service import AgentWorkflowService


class TaskDagBuildWorkflow:
    name = TASK_DAG_WORKFLOW

    def execute(
        self,
        service: AgentWorkflowService,
        generation_request: SkillGenerationRequest,
        agent_run: AgentRun,
        permission_plan: dict[str, Any],
    ) -> tuple[AgentRun, Skill, Any]:
        api_index = backend_api_index()
        permission_bounds = service._agent_permission_bounds(permission_plan)
        task_dag = service.codex_service.product_manager_write_task_dag(
            generation_request,
            agent_run.blueprint_json or {},
            permission_bounds,
            api_index,
            prompt_builder=build_product_manager_prompt,
        )
        service.task_dags.validate(task_dag, agent_run.blueprint_json or {})
        task_dag_path = service.artifacts.write_json(agent_run, "task_dag.json", task_dag)
        task_paths = service.artifacts.write_task_artifacts(agent_run, task_dag)
        task_ids = [node["id"] for node in service.task_dags.nodes(task_dag)]
        agent_run.failure_count_json = {task_id: 0 for task_id in task_ids}
        agent_run.status = "running"
        agent_run.current_step = "product_manager"
        agent_run.summary = "ProductManager wrote and backend validated the task DAG."
        service._finish_step(
            agent_run,
            service._start_step(
                agent_run,
                "product_manager",
                input_json={
                    "action": "pm_write_task_dag",
                    "blueprint_json": agent_run.blueprint_json,
                    "permission_bounds": permission_bounds,
                    "backend_api_index": api_index,
                    "backend_api_index_file": backend_api_index_file().as_posix(),
                    "integration_operation_index": operation_index(),
                },
                logs="ProductManager wrote task_dag.json after build-time approval.",
            ),
            "succeeded",
            output_json={
                "task_dag_json": task_dag,
                "task_dag_path": task_dag_path,
                "task_paths": task_paths,
                "decision_json": {"decision": "run_ready_task_nodes"},
            },
            logs="Backend validated the task DAG and wrote task node artifacts.",
        )
        agent_run.current_step = "builder"
        service.db.commit()

        skill: Skill | None = None
        validation: Any = None
        task_statuses = {task_id: "pending" for task_id in task_ids}
        task_batches = service.task_dags.execution_batches(task_dag)
        batch_starts = {batch[0]["id"]: batch for batch in task_batches}
        batch_ends = {batch[-1]["id"] for batch in task_batches}
        for task_node in [node for batch in task_batches for node in batch]:
            task_id = task_node["id"]
            if task_id in batch_starts:
                if skill is not None and service._pause_if_usage_below_reserve(agent_run):
                    return agent_run, skill, validation
                for ready_node in batch_starts[task_id]:
                    task_statuses[ready_node["id"]] = "ready"
                service.artifacts.write_task_statuses(agent_run, task_statuses)
            task_statuses[task_id] = "building"
            service.artifacts.write_task_statuses(agent_run, task_statuses)
            agent_run.current_task_id = task_id
            service.db.commit()
            builder_step = service._start_step(
                agent_run,
                "builder",
                task_node_id=task_id,
                input_json={"mode": "build_task", **service._builder_task_context(agent_run, task_node, skill)},
                logs=f"Builder is implementing task node {task_id} only.",
            )
            try:
                if skill is None:
                    skill, validation = service.codex_service.generate_from_request(
                        generation_request,
                        builder_writes_tests=False,
                        initial_skill_status="building",
                        task_context=service._builder_task_context(agent_run, task_node, skill),
                        create_runtime_request=False,
                        workspace_prepared=True,
                    )
                    builder_output = {"skill_id": skill.id, "skill_name": skill.name, "mode": "build_task"}
                else:
                    result, validation = service.codex_service.build_skill_task(
                        skill,
                        generation_request,
                        service._builder_task_context(agent_run, task_node, skill),
                    )
                    builder_output = {
                        "skill_id": skill.id,
                        "skill_name": skill.name,
                        "mode": "build_task",
                        **service._subprocess_output(result),
                        "exit_code": result.returncode,
                    }
            except CodexGenerationError as exc:
                if "USER_ACTION_REQUIRED:" in str(exc):
                    service._builder_user_action_required(agent_run, builder_step, str(exc), task_id)
                    raise ProjectBuildWorkflowError(str(exc)) from exc
                raise
            interface_artifact_path = service.artifacts.move_validated_interface_artifact(
                agent_run,
                service.proposed_service.skill_dir_for_record(skill),
                task_node,
            )
            builder_output["interface_artifact_path"] = interface_artifact_path
            service._finish_step(
                agent_run,
                builder_step,
                "succeeded",
                output_json=builder_output,
                logs=f"Builder completed task node {task_id}. The skill was not installed or run.",
            )

            if task_node.get("requires_tests"):
                task_statuses[task_id] = "testing"
                service.artifacts.write_task_statuses(agent_run, task_statuses)
                validation = service._test_task_node(agent_run, skill, validation, task_node_id=task_id)
            else:
                validation = service.proposed_service.validate_proposed_skill(skill)
            while not validation.ok:
                task_statuses[task_id] = "fixing"
                service.artifacts.write_task_statuses(agent_run, task_statuses)
                service._increment_failure_count(agent_run, task_id)
                if service._failure_count(agent_run, task_id) > MAX_TASK_FAILURES:
                    service._product_manager_stop_failed(agent_run, skill, validation)
                    return agent_run, skill, validation
                validation = service._repair_current_task(agent_run, skill, validation, task_node_id=task_id)

            task_statuses[task_id] = "done"
            service.artifacts.write_task_statuses(agent_run, task_statuses)
            if task_id in batch_ends and service._pause_if_usage_below_reserve(agent_run):
                return agent_run, skill, validation

        if skill is None:
            raise ProjectBuildWorkflowError("No task DAG nodes were available")
        service._write_final_e2e_test(agent_run, skill)
        validation = service._run_final_validation(agent_run, skill)
        while not validation.ok:
            service._increment_failure_count(agent_run, FINAL_E2E_FAILURE_KEY)
            if service._failure_count(agent_run, FINAL_E2E_FAILURE_KEY) > MAX_TASK_FAILURES:
                service._stop_final_validation_failed(agent_run, skill, validation)
                return agent_run, skill, validation
            validation = service._repair_final_e2e(agent_run, skill, validation)
        pm_runtime_summary = service._pm_runtime_summary(skill, validation)
        runtime_status = service._runtime_permission_review(
            agent_run,
            skill,
            validation,
            pm_summary=pm_runtime_summary,
        )
        service._product_manager_finish(
            agent_run,
            skill,
            validation,
            runtime_status,
            pm_summary=pm_runtime_summary,
        )
        service.db.refresh(agent_run)
        return agent_run, skill, validation

    def resume(self, service: AgentWorkflowService, agent_run: AgentRun) -> AgentRun:
        generation_request = service.db.get(SkillGenerationRequest, agent_run.generation_request_id)
        if generation_request is None or generation_request.proposed_skill_id is None:
            raise ProjectBuildWorkflowError("Paused build no longer has a proposed skill")
        skill = service.db.get(Skill, generation_request.proposed_skill_id)
        if skill is None:
            raise ProjectBuildWorkflowError("Paused build's proposed skill no longer exists")
        task_dag = service.artifacts.read_json(agent_run, "task_dag.json")
        if not service.task_dags.nodes(task_dag):
            raise ProjectBuildWorkflowError("Paused build task DAG is unavailable")
        task_statuses = dict((agent_run.final_summary_json or {}).get("task_statuses", {}))
        agent_run.status = "running"
        agent_run.pause_reason = None
        agent_run.error_message = None
        service.db.commit()

        validation: Any = service.proposed_service.validate_proposed_skill(skill)
        completed_task_ids = {task_id for task_id, status in task_statuses.items() if status == "done"}
        task_batches = service.task_dags.execution_batches(task_dag, completed_task_ids=completed_task_ids)
        batch_starts = {batch[0]["id"]: batch for batch in task_batches}
        batch_ends = {batch[-1]["id"] for batch in task_batches}
        for task_node in [node for batch in task_batches for node in batch]:
            task_id = task_node["id"]
            if task_id in batch_starts:
                if service._pause_if_usage_below_reserve(agent_run):
                    return agent_run
                for ready_node in batch_starts[task_id]:
                    task_statuses[ready_node["id"]] = "ready"
                service.artifacts.write_task_statuses(agent_run, task_statuses)
            task_statuses[task_id] = "building"
            service.artifacts.write_task_statuses(agent_run, task_statuses)
            builder_step = service._start_step(
                agent_run,
                "builder",
                task_node_id=task_id,
                input_json={"mode": "build_task", **service._builder_task_context(agent_run, task_node, skill)},
                logs=f"Builder is resuming task node {task_id} only.",
            )
            result, validation = service.codex_service.build_skill_task(
                skill,
                generation_request,
                service._builder_task_context(agent_run, task_node, skill),
            )
            interface_artifact_path = service.artifacts.move_validated_interface_artifact(
                agent_run,
                service.proposed_service.skill_dir_for_record(skill),
                task_node,
            )
            service._finish_step(
                agent_run,
                builder_step,
                "succeeded",
                output_json={
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "mode": "build_task",
                    **service._subprocess_output(result),
                    "exit_code": result.returncode,
                    "interface_artifact_path": interface_artifact_path,
                },
                logs=f"Builder completed resumed task node {task_id}.",
            )
            if task_node.get("requires_tests"):
                task_statuses[task_id] = "testing"
                service.artifacts.write_task_statuses(agent_run, task_statuses)
                validation = service._test_task_node(agent_run, skill, validation, task_node_id=task_id)
            else:
                validation = service.proposed_service.validate_proposed_skill(skill)
            while not validation.ok:
                task_statuses[task_id] = "fixing"
                service.artifacts.write_task_statuses(agent_run, task_statuses)
                service._increment_failure_count(agent_run, task_id)
                if service._failure_count(agent_run, task_id) > MAX_TASK_FAILURES:
                    service._product_manager_stop_failed(agent_run, skill, validation)
                    return agent_run
                validation = service._repair_current_task(agent_run, skill, validation, task_node_id=task_id)
            task_statuses[task_id] = "done"
            service.artifacts.write_task_statuses(agent_run, task_statuses)
            if task_id in batch_ends and service._pause_if_usage_below_reserve(agent_run):
                return agent_run

        service._write_final_e2e_test(agent_run, skill)
        validation = service._run_final_validation(agent_run, skill)
        while not validation.ok:
            service._increment_failure_count(agent_run, FINAL_E2E_FAILURE_KEY)
            if service._failure_count(agent_run, FINAL_E2E_FAILURE_KEY) > MAX_TASK_FAILURES:
                service._stop_final_validation_failed(agent_run, skill, validation)
                return agent_run
            validation = service._repair_final_e2e(agent_run, skill, validation)
        pm_runtime_summary = service._pm_runtime_summary(skill, validation)
        runtime_status = service._runtime_permission_review(
            agent_run,
            skill,
            validation,
            pm_summary=pm_runtime_summary,
        )
        service._product_manager_finish(
            agent_run,
            skill,
            validation,
            runtime_status,
            pm_summary=pm_runtime_summary,
        )
        service.db.refresh(agent_run)
        return agent_run

    def retry_failed(self, service: AgentWorkflowService, agent_run: AgentRun) -> AgentRun:
        generation_request = service.db.get(SkillGenerationRequest, agent_run.generation_request_id)
        skill = service.db.get(Skill, agent_run.skill_id)
        if generation_request is None:
            raise ProjectBuildWorkflowError("Generation request no longer exists")
        resumable_artifact = (
            service.artifacts.directory(agent_run)
            / "tasks"
            / str(agent_run.current_task_id or "")
            / "interface_artifact.json"
        )
        if skill is None or not agent_run.current_task_id or not resumable_artifact.is_file():
            generation_request.status = "approved"
            generation_request.error_message = None
            service.db.commit()
            service.continue_build_after_approval(generation_request)
            service.db.refresh(agent_run)
            return agent_run
        task_id = agent_run.current_task_id
        generation_request.status = "approved"
        generation_request.error_message = None
        skill.status = "building"
        agent_run.status = "running"
        agent_run.error_message = None
        agent_run.completed_at = None
        service.db.commit()

        validation = service.proposed_service.validate_proposed_skill(skill)
        service._increment_failure_count(agent_run, task_id)
        if service._failure_count(agent_run, task_id) > MAX_TASK_FAILURES:
            service._product_manager_stop_failed(agent_run, skill, validation)
            return agent_run
        validation = service._repair_current_task(agent_run, skill, validation, task_node_id=task_id)
        while not validation.ok:
            service._increment_failure_count(agent_run, task_id)
            if service._failure_count(agent_run, task_id) > MAX_TASK_FAILURES:
                service._product_manager_stop_failed(agent_run, skill, validation)
                return agent_run
            validation = service._repair_current_task(agent_run, skill, validation, task_node_id=task_id)

        task_statuses = dict((agent_run.final_summary_json or {}).get("task_statuses", {}))
        task_statuses[task_id] = "done"
        service.artifacts.write_task_statuses(agent_run, task_statuses)
        return self.resume(service, agent_run)
