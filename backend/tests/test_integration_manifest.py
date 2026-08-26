from pathlib import Path

import pytest

from app.services.default_permissions import planning_permission_policy
from app.services.integration_registry import OPERATIONS
from app.services.manifest_validator import ManifestValidationError, validate_manifest
from app.services.task_dag_service import TaskDagService
from app.workflows.base import ProjectBuildWorkflowError
from app.workflows.common.prompts import build_product_manager_prompt
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
    }
    value.update(overrides)
    return value


def test_registry_is_authoritative_and_context_is_selected_only() -> None:
    assert {operation_id for operation_id in OPERATIONS if operation_id.startswith("github.")} == {
        "github.repository.get",
        "github.repository.tree.list",
        "github.repository.file.read",
        "github.issue.list",
        "github.pull_request.list",
        "github.repository.trending.list",
    }
    assert {operation_id for operation_id in OPERATIONS if operation_id.startswith("atlas.")} == {
        "atlas.person.get",
        "atlas.experience.list",
        "atlas.goal.list",
        "atlas.project.list",
        "atlas.relationship.list",
        "atlas.knowledge.frontier.list",
        "atlas.knowledge.search",
        "atlas.knowledge.node.get",
        "atlas.knowledge.node.know",
    }
    assert {operation_id for operation_id in OPERATIONS if operation_id.startswith("notion.")} == {
        "notion.todo.list",
        "notion.todo.create",
        "notion.todo.update",
        "notion.todo.delete",
    }
    context = OPERATIONS["github.repository.file.read"].agent_context()
    assert context["operation"] == "github.repository.file.read"
    assert context["input_schema"] == OPERATIONS["github.repository.file.read"].input_schema
    assert "endpoint_template" not in context


def test_product_manager_and_single_codex_context_are_minimized(tmp_path) -> None:
    product_manager_prompt = build_product_manager_prompt(
        "plan_build",
        {
            "intent_prompt": {"refined_prompt": "Build a GitHub repository reader."},
            "permission_policy": planning_permission_policy(),
            "function_catalog_index": [
                {
                    "id": "github.repository.get",
                    "category": "integration",
                    "title": "Read repository",
                    "description": "Read repository metadata.",
                }
            ],
        },
    )
    assert "github.repository.get" in product_manager_prompt
    payload_section = product_manager_prompt.rsplit("Payload:", 1)[1]
    assert '"input_schema"' not in payload_section
    assert "endpoint_template" not in product_manager_prompt
    single_prompt = build_prompt(
        {
            "functions": ["github.repository.get"],
            "function_context": [OPERATIONS["github.repository.get"].agent_context()],
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


def test_manifest_accepts_separate_github_and_atlas_requirements() -> None:
    manifest = manifest_with(github_requirement())
    manifest["integration_requirements"].append(
        {
            "provider": "atlas",
            "operations": ["atlas.project.list", "atlas.knowledge.search"],
            "resource_scope": {},
        }
    )

    parsed = validate_manifest(manifest)

    assert [item.provider for item in parsed.integration_requirements] == ["github", "atlas"]


def test_manifest_accepts_notion_without_caller_selected_scope() -> None:
    parsed = validate_manifest(
        manifest_with(
            {
                "provider": "notion",
                "operations": ["notion.todo.list", "notion.todo.create"],
                "resource_scope": {},
            }
        )
    )
    requirement = parsed.integration_requirements[0]
    assert requirement.provider == "notion"
    assert requirement.resource_scope.repositories == []
    assert OPERATIONS["notion.todo.list"].risk == "low"
    assert OPERATIONS["notion.todo.create"].risk == "medium"
    assert OPERATIONS["notion.todo.list"].output_schema["properties"]["todos"]["items"]["properties"][
        "done"
    ] == {"type": "boolean"}
    assert OPERATIONS["notion.todo.create"].input_schema["properties"]["done"] == {"type": "boolean"}
    assert OPERATIONS["notion.todo.update"].input_schema["properties"]["done"] == {"type": "boolean"}
    assert OPERATIONS["notion.todo.list"].contract_version == 2


def test_manifest_rejects_notion_operation_under_another_provider() -> None:
    with pytest.raises(ManifestValidationError, match="must match"):
        validate_manifest(
            manifest_with(
                {
                    "provider": "github",
                    "operations": ["notion.todo.list"],
                    "resource_scope": {},
                }
            )
        )


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
                "task_prompt": "Build the core skill.",
                "acceptance_criteria": ["works"],
                "write_paths": ["skill.py"],
                "requires_tests": True,
                "function_ids": ["github.issue.list"],
            }
        ],
    }
    blueprint = {"functions": ["github.repository.get"]}
    with pytest.raises(ProjectBuildWorkflowError, match="unapproved function"):
        TaskDagService().validate(task_dag, blueprint)


def test_task_dag_must_assign_every_approved_operation() -> None:
    task_dag = {
        "schema_version": 1,
        "nodes": [
            {
                "id": "core",
                "task_prompt": "Build the core skill.",
                "acceptance_criteria": ["works"],
                "write_paths": ["skill.py"],
                "requires_tests": True,
                "function_ids": [],
            }
        ],
    }
    blueprint = {"functions": ["github.repository.get"]}
    with pytest.raises(ProjectBuildWorkflowError, match="does not assign approved functions"):
        TaskDagService().validate(task_dag, blueprint)


def test_settings_routes_are_absent_from_function_catalog_seed() -> None:
    static_root = Path(__file__).resolve().parents[1] / "app" / "static"
    combined = (static_root / "function_catalog_seed.json").read_text(encoding="utf-8")
    assert "/settings/integrations" not in combined
    assert "secret_reference" not in combined
