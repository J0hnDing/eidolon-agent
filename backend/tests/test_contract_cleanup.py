from typing import get_args

from app.main import app
from app.schemas.agent_run import AgentRunRead, AgentRunStepRead
from app.schemas.common import ScheduleStatus, SkillStatus


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
