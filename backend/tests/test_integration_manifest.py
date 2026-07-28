from pathlib import Path

import pytest

from app.services.integration_registry import OPERATIONS, operation_context, operation_index
from app.services.manifest_validator import ManifestValidationError, validate_manifest
from app.services.skill_plan_service import SkillPlanService
from app.services.task_dag_service import TaskDagService
from app.workflows.base import ProjectBuildWorkflowError
from app.workflows.single_codex.prompts import build_prompt


def manifest_with(requirement: dict) -> dict:
    return {
        "manifest_version": 1,
        "name": "github_reader",
        "description": "Read approved GitHub data.",
        "runtime": "function",
        "entrypoint": "skill.py",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "function_requirements": [],
        "integration_requirements": [requirement],
        "dependencies": [],
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": [],
            "secrets": [],
            "shell": False,
        },
    }


def github_requirement(**overrides) -> dict:
    value = {
        "provider": "github",
        "operations": ["github.repository.get", "github.repository.file.read"],
        "resource_scope": {"repositories": ["OpenAI/Eidolon"]},
        "reason": "Read repository metadata and selected files.",
    }
    value.update(overrides)
    return value


def test_registry_is_authoritative_and_context_is_selected_only() -> None:
    assert set(OPERATIONS) == {
        "github.repository.get",
        "github.repository.tree.list",
        "github.repository.file.read",
        "github.issue.list",
        "github.pull_request.list",
        "github.repository.trending.list",
    }
    assert all(item["operation"] in OPERATIONS for item in operation_index())
    context = operation_context(["github.repository.file.read", "unknown"])
    assert [item["operation"] for item in context] == ["github.repository.file.read"]
    assert context[0]["input_schema"] == OPERATIONS["github.repository.file.read"].input_schema
    assert "endpoint_template" not in context[0]
    assert all(set(item) == {"operation", "title", "description"} for item in operation_index())


def test_product_manager_and_single_codex_context_are_minimized(tmp_path) -> None:
    product_manager_prompt = SkillPlanService().build_prompt("Build a GitHub repository reader.")
    index_section = product_manager_prompt.split(
        "Current GitHub integration operation index", 1
    )[1].split("User Project mode request:", 1)[0]
    assert "github.repository.get" in index_section
    assert '"input_schema"' not in index_section
    assert "endpoint_template" not in index_section
    single_prompt = build_prompt(
        {
            "integration_requirements": [
                github_requirement(operations=["github.repository.get"])
            ]
        },
        {},
        tmp_path,
    )
    assert '"operation": "github.repository.get"' in single_prompt
    assert '"operation": "github.issue.list"' not in single_prompt


def test_manifest_normalizes_exact_repository_scope() -> None:
    manifest = validate_manifest(manifest_with(github_requirement()))
    requirement = manifest.integration_requirements[0]
    assert requirement.resource_scope.repositories == ["openai/eidolon"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"operations": ["github.repository.get", "github.repository.get"]}, "duplicate"),
        ({"operations": ["github.unknown"]}, "unknown integration"),
        ({"resource_scope": {"repositories": ["openai/*"]}}, "exact owner/repository"),
        ({"resource_scope": {"repositories": ["openai/eidolon", "OpenAI/Eidolon"]}}, "duplicates"),
        ({"resource_scope": {"repositories": []}}, "require exact repository scope"),
        ({"reason": "x" * 301}, "at most 300"),
        ({"reason": "line one\nline two"}, "concise user-readable"),
        (
            {
                "operations": ["github.repository.trending.list"],
                "resource_scope": {"repositories": ["openai/eidolon"]},
            },
            "cannot declare repository scope",
        ),
    ],
)
def test_manifest_rejects_invalid_integration_contract(overrides: dict, expected: str) -> None:
    with pytest.raises(ManifestValidationError, match=expected):
        validate_manifest(manifest_with(github_requirement(**overrides)))


def test_trending_uses_registry_defined_non_repository_scope() -> None:
    manifest = validate_manifest(
        manifest_with(
            github_requirement(
                operations=["github.repository.trending.list"],
                resource_scope={"repositories": []},
            )
        )
    )
    assert manifest.integration_requirements[0].resource_scope.repositories == []


def test_task_dag_rejects_operation_outside_approved_blueprint() -> None:
    task_dag = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "core",
                "acceptance_criteria": ["works"],
                "expected_output_paths": ["skill.py"],
                "file_write_claims": ["skill.py"],
                "requires_tests": True,
                "integration_operation_ids": ["github.issue.list"],
            }
        ],
    }
    blueprint = {"integration_requirements": [github_requirement(operations=["github.repository.get"])]}
    with pytest.raises(ProjectBuildWorkflowError, match="unapproved integration operation"):
        TaskDagService().validate(task_dag, blueprint)


def test_task_dag_must_assign_every_approved_operation() -> None:
    task_dag = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "core",
                "acceptance_criteria": ["works"],
                "expected_output_paths": ["skill.py"],
                "file_write_claims": ["skill.py"],
                "requires_tests": True,
                "integration_operation_ids": [],
            }
        ],
    }
    blueprint = {"integration_requirements": [github_requirement(operations=["github.repository.get"])]}
    with pytest.raises(ProjectBuildWorkflowError, match="does not assign approved integration operations"):
        TaskDagService().validate(task_dag, blueprint)


def test_settings_routes_are_absent_from_agent_backend_catalogs() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    combined = "\n".join(
        (static_root / filename).read_text(encoding="utf-8")
        for filename in ("backend_api_index.json", "backend_api_context.json")
    )
    assert "/settings/integrations" not in combined
    assert "secret_reference" not in combined
