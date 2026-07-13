from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.models import AgentRun, Skill, SkillGenerationRequest
    from app.services.agent_workflow_service import AgentWorkflowService


TASK_DAG_WORKFLOW = "task_dag"
SINGLE_CODEX_WORKFLOW = "single_codex"
DEFAULT_BUILD_WORKFLOW = TASK_DAG_WORKFLOW
SUPPORTED_BUILD_WORKFLOWS = frozenset({TASK_DAG_WORKFLOW, SINGLE_CODEX_WORKFLOW})

MAX_TASK_FAILURES = 3
FINAL_E2E_FAILURE_KEY = "__final_e2e__"


class ProjectBuildWorkflowError(ValueError):
    pass


class ProjectBuildWorkflow(Protocol):
    name: str

    def execute(
        self,
        service: AgentWorkflowService,
        generation_request: SkillGenerationRequest,
        agent_run: AgentRun,
        permission_plan: dict[str, Any],
    ) -> tuple[AgentRun, Skill, Any]: ...

    def resume(self, service: AgentWorkflowService, agent_run: AgentRun) -> AgentRun: ...

    def retry_failed(self, service: AgentWorkflowService, agent_run: AgentRun) -> AgentRun: ...
