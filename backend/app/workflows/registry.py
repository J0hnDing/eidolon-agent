from app.workflows.base import ProjectBuildWorkflow, ProjectBuildWorkflowError
from app.workflows.single_codex.workflow import SingleCodexBuildWorkflow
from app.workflows.task_dag.workflow import TaskDagBuildWorkflow


_PROJECT_BUILD_WORKFLOWS: dict[str, ProjectBuildWorkflow] = {
    TaskDagBuildWorkflow.name: TaskDagBuildWorkflow(),
    SingleCodexBuildWorkflow.name: SingleCodexBuildWorkflow(),
}


def get_project_build_workflow(name: str) -> ProjectBuildWorkflow:
    workflow = _PROJECT_BUILD_WORKFLOWS.get(name)
    if workflow is None:
        raise ProjectBuildWorkflowError(f"Unknown project build workflow: {name}")
    return workflow
