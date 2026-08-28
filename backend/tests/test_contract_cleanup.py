from pathlib import Path
from typing import get_args

import pytest
from jsonschema import Draft202012Validator

from app.main import app
from app.routers.codex_settings import get_permission_policy
from app.schemas.agent_run import AgentRunRead, AgentRunStepRead
from app.schemas.common import ScheduleStatus, SkillRuntime, SkillStatus
from app.schemas.manifest import SkillManifest
from app.schemas.permission_policy import PermissionPolicyRead
from app.schemas.skill import SkillRead
from app.services import codex_output_schema
from app.services.codex_output_schema import output_schema_for_action
from app.services.default_permissions import (
    approval_required_permissions,
    blocked_permissions,
    default_allowed_permissions,
    default_permission_policy,
    default_web_app_permissions,
    planning_permission_policy,
)
from app.workflows.base import ProjectBuildWorkflowError
from app.workflows.common.prompts import build_product_manager_prompt


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
    assert set(get_args(SkillRuntime)) == {"function", "service", "web_app"}
    for schema in (SkillManifest, SkillRead):
        assert "interface_type" not in schema.model_fields
        assert "tool_ui_schema" not in schema.model_fields
        assert "tool_ui_schema_json" not in schema.model_fields
    assert not any(path == "/tools" or path.startswith("/tools/") for path in app.openapi()["paths"])


def test_default_web_app_permissions_match_runtime_support() -> None:
    assert default_web_app_permissions() == {
        "supported": [
            "scripts",
            "forms",
            "same_origin",
            "modals",
            "approved_server_network",
            "approved_server_codex",
        ],
        "blocked": [
            "external_browser_network",
            "top_navigation",
            "popups",
            "downloads",
            "privileged_browser_features",
            "websockets",
        ],
    }


def test_permission_config_drives_agent_policy_and_output_schema() -> None:
    policy = planning_permission_policy()

    assert policy == {
        "default_allowed": default_allowed_permissions(),
        "requires_approval": approval_required_permissions(),
        "blocked": blocked_permissions(),
    }
    permission_schema = output_schema_for_action("product_manager_plan_build")["properties"][
        "permission_plan"
    ]["anyOf"][1]
    assert set(permission_schema["properties"]) == set(approval_required_permissions())
    assert set(permission_schema["properties"]["runtime"]["properties"]) == set(
        approval_required_permissions()["runtime"]
    )
    assert "call_response" not in default_allowed_permissions()["runtime"].get("codex", {})
    assert approval_required_permissions()["runtime"]["codex"]["call_response"] is False
    mutable_copy = default_permission_policy()
    mutable_copy["blocked"].clear()
    assert blocked_permissions()


def test_settings_permission_policy_endpoint_returns_the_current_config() -> None:
    payload = get_permission_policy()
    parsed = PermissionPolicyRead.model_validate(payload)

    assert "/settings/permission-policy" in app.openapi()["paths"]
    assert parsed.source == "backend/app/static/default_permissions.json"
    assert parsed.model_dump(exclude={"source"}) == default_permission_policy()


def test_permission_plan_schema_strictly_validates_the_config_template() -> None:
    schema = codex_output_schema.permission_plan_output_schema()
    validator = Draft202012Validator(schema)
    valid_plan = approval_required_permissions()

    assert validator.is_valid(valid_plan)
    assert "uniqueItems" not in schema["properties"]["build_time"]["properties"]["dependencies"]
    assert "uniqueItems" not in schema["properties"]["runtime"]["properties"]["dependencies"]
    assert "uniqueItems" not in schema["properties"]["runtime"]["properties"]["network"]
    assert validator.is_valid({**valid_plan, "unknown": True}) is False
    assert validator.is_valid({"build_time": valid_plan["build_time"]}) is False
    invalid_runtime = {**valid_plan["runtime"], "network": [""]}
    assert validator.is_valid({**valid_plan, "runtime": invalid_runtime}) is False


def test_product_manager_schemas_rederive_permission_shape_and_return_independent_copies(
    monkeypatch,
) -> None:
    template = {
        "build": {"research": False},
        "runtime": {"domains": [], "nested": {"enabled": False}},
    }
    monkeypatch.setattr(codex_output_schema, "approval_required_permissions", lambda: template)

    build_schema = codex_output_schema.output_schema_for_action("product_manager_plan_build")
    update_schema = codex_output_schema.output_schema_for_action("product_manager_update_review")
    assert build_schema["properties"]["permission_plan"]["anyOf"][1] == (
        codex_output_schema.permission_plan_output_schema()
    )
    assert update_schema["properties"]["blueprint"]["properties"]["permission_plan"] == (
        codex_output_schema.permission_plan_output_schema()
    )

    build_schema["properties"]["permission_plan"]["anyOf"][1]["properties"].clear()
    fresh_schema = codex_output_schema.output_schema_for_action("product_manager_plan_build")
    assert set(fresh_schema["properties"]["permission_plan"]["anyOf"][1]["properties"]) == {
        "build",
        "runtime",
    }


def test_permission_policy_values_are_not_duplicated_in_agent_instructions() -> None:
    instruction_roots = [
        Path("app/agent_instructions"),
        Path("app/workflows/common/instructions"),
        Path("app/workflows/task_dag/instructions"),
        Path("app/workflows/single_codex/instructions"),
    ]
    instruction_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in instruction_roots
        for path in root.rglob("*.md")
    )

    for blocked in blocked_permissions():
        assert blocked not in instruction_text


def test_schema_backed_instructions_do_not_duplicate_output_syntax() -> None:
    instruction_paths = [
        Path("app/workflows/common/instructions/plan_build.md"),
        Path("app/workflows/task_dag/instructions/product_manager.md"),
        Path("app/agent_instructions/product_manager/repair.md"),
        Path("app/agent_instructions/product_manager/update.md"),
    ]

    for path in instruction_paths:
        instruction = path.read_text(encoding="utf-8")
        assert "matching the supplied output schema" in instruction
        assert "Required JSON shape" not in instruction
        assert "Expected JSON syntax" not in instruction
        assert "For `write_task_dag`, return" not in instruction


@pytest.mark.parametrize(
    "legacy_task",
    ["build_blueprint", "write_blueprint", "write_permissions"],
)
def test_legacy_product_manager_prompt_aliases_are_removed(legacy_task: str) -> None:
    with pytest.raises(ProjectBuildWorkflowError, match="Unknown common project-build prompt task"):
        build_product_manager_prompt(legacy_task, {})


def test_standalone_project_plausibility_contract_is_removed() -> None:
    assert not Path("app/services/project_plausibility.py").exists()
    assert output_schema_for_action("project_plausibility") is None
