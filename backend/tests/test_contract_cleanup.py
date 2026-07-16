from typing import get_args

from app.main import app
from app.schemas.agent_run import AgentRunRead, AgentRunStepRead
from app.schemas.common import ScheduleStatus, SkillRuntime, SkillStatus
from app.schemas.manifest import SkillManifest
from app.schemas.skill import SkillRead
from app.services.skill_plan_service import SkillGenerationPlan


def test_agent_run_contract_uses_only_task_node_names() -> None:
    assert "current_task_id" in AgentRunRead.model_fields
    assert "current_milestone" not in AgentRunRead.model_fields
    assert "task_node_id" in AgentRunStepRead.model_fields
    assert "milestone_name" not in AgentRunStepRead.model_fields
    assert "/agent-runs/{agent_run_id}/retry-current-task" in app.openapi()["paths"]
    assert "/agent-runs/{agent_run_id}/retry-current-milestone" not in app.openapi()["paths"]


def test_status_contracts_do_not_advertise_non_persisted_states() -> None:
    assert "disabled" not in get_args(SkillStatus)
    assert "deleted" not in get_args(ScheduleStatus)


def test_runtime_is_the_only_interface_discriminator() -> None:
    assert set(get_args(SkillRuntime)) == {"function", "web_app"}
    for schema in (SkillGenerationPlan, SkillManifest, SkillRead):
        assert "interface_type" not in schema.model_fields
        assert "tool_ui_schema" not in schema.model_fields
        assert "tool_ui_schema_json" not in schema.model_fields
    assert not any(path == "/tools" or path.startswith("/tools/") for path in app.openapi()["paths"])
