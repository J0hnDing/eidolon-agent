from __future__ import annotations

import re
from collections.abc import Iterable
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ProviderSpec,
    ResourceSpec,
    RiskLevel,
)

REPOSITORY_PROPERTIES = {
    "owner": {"type": "string", "pattern": "^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"},
    "repository": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,100}$"},
}


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_COMMON_ERRORS = (
    "connection_unavailable",
    "invalid_credential",
    "operation_undeclared",
    "authorization_missing_or_stale",
    "repository_outside_scope",
    "invalid_input",
    "not_found",
    "provider_forbidden",
    "rate_limited",
    "provider_timeout",
    "response_too_large",
    "provider_unavailable",
    "internal_failure",
)
_REPOSITORY_OUTPUT = _object_schema(
    {
        "full_name": {"type": "string"},
        "description": {"type": ["string", "null"]},
        "private": {"type": "boolean"},
        "default_branch": {"type": "string"},
        "html_url": {"type": "string"},
        "stars": {"type": "integer", "minimum": 0},
        "forks": {"type": "integer", "minimum": 0},
        "open_issues": {"type": "integer", "minimum": 0},
        "updated_at": {"type": "string"},
    },
    [
        "full_name",
        "description",
        "private",
        "default_branch",
        "html_url",
        "stars",
        "forks",
        "open_issues",
        "updated_at",
    ],
)
_TRENDING_REPOSITORY_OUTPUT = _object_schema(
    {
        "rank": {"type": "integer", "minimum": 1, "maximum": 25},
        "full_name": {"type": "string"},
        "description": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "html_url": {"type": "string"},
        "stars": {"type": "integer", "minimum": 0},
        "forks": {"type": "integer", "minimum": 0},
        "stars_gained": {"type": "integer", "minimum": 0},
        "readme": {"type": ["string", "null"]},
        "readme_truncated": {"type": "boolean"},
    },
    [
        "rank",
        "full_name",
        "description",
        "language",
        "html_url",
        "stars",
        "forks",
        "stars_gained",
        "readme",
        "readme_truncated",
    ],
)
_ISSUE_OUTPUT = _object_schema(
    {
        "number": {"type": "integer", "minimum": 1},
        "title": {"type": "string"},
        "state": {"enum": ["open", "closed"]},
        "html_url": {"type": "string"},
        "author": {"type": "string"},
        "labels": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
    },
    ["number", "title", "state", "html_url", "author", "labels", "created_at", "updated_at"],
)
_PULL_REQUEST_OUTPUT = _object_schema(
    {
        "number": {"type": "integer", "minimum": 1},
        "title": {"type": "string"},
        "state": {"enum": ["open", "closed"]},
        "draft": {"type": "boolean"},
        "html_url": {"type": "string"},
        "author": {"type": "string"},
        "head": {"type": "string"},
        "base": {"type": "string"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
    },
    ["number", "title", "state", "draft", "html_url", "author", "head", "base", "created_at", "updated_at"],
)
_GITHUB_OPERATIONS = (
    IntegrationOperationSpec(
        id="github.repository.get",
        provider_id="github",
        title="Get repository",
        description="Read normalized metadata for one approved GitHub repository.",
        input_schema=_object_schema(dict(REPOSITORY_PROPERTIES), ["owner", "repository"]),
        output_schema=_REPOSITORY_OUTPUT,
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("github.repository", ("owner", "repository")),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "github.repository.get", "input": {"owner": "octo", "repository": "demo"}},
            normalized_errors=_COMMON_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="github.repository.tree.list",
        provider_id="github",
        title="List repository tree",
        description="List a bounded, depth-limited repository tree and report truncation.",
        input_schema=_object_schema(
            {
                **REPOSITORY_PROPERTIES,
                "ref": {"type": "string", "minLength": 1, "maxLength": 200, "default": "HEAD"},
                "path": {
                    "type": "string",
                    "maxLength": 500,
                    "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$)).*$",
                    "default": "",
                },
                "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
            ["owner", "repository"],
        ),
        output_schema=_object_schema(
            {
                "repository": {"type": "string"},
                "ref": {"type": "string"},
                "entries": {
                    "type": "array",
                    "maxItems": 500,
                    "items": _object_schema(
                        {
                            "path": {"type": "string"},
                            "type": {"enum": ["blob", "tree"]},
                            "size": {"type": ["integer", "null"]},
                        },
                        ["path", "type", "size"],
                    ),
                },
                "truncated": {"type": "boolean"},
            },
            ["repository", "ref", "entries", "truncated"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("github.repository", ("owner", "repository")),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={
                "operation": "github.repository.tree.list",
                "input": {"owner": "octo", "repository": "demo", "depth": 2},
            },
            normalized_errors=_COMMON_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="github.repository.file.read",
        provider_id="github",
        title="Read repository file",
        description="Read one bounded UTF-8 text file from an approved repository.",
        input_schema=_object_schema(
            {
                **REPOSITORY_PROPERTIES,
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$)).+$",
                },
                "ref": {"type": "string", "minLength": 1, "maxLength": 200, "default": "HEAD"},
            },
            ["owner", "repository", "path"],
        ),
        output_schema=_object_schema(
            {
                "repository": {"type": "string"},
                "path": {"type": "string"},
                "ref": {"type": "string"},
                "text": {"type": "string", "maxLength": 262144},
                "size": {"type": "integer", "minimum": 0, "maximum": 262144},
                "sha": {"type": "string"},
            },
            ["repository", "path", "ref", "text", "size", "sha"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("github.repository", ("owner", "repository")),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={
                "operation": "github.repository.file.read",
                "input": {"owner": "octo", "repository": "demo", "path": "README.md"},
            },
            normalized_errors=_COMMON_ERRORS + ("unsupported_file_type",),
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="github.issue.list",
        provider_id="github",
        title="List issues",
        description="List a bounded set of normalized issues from one approved repository.",
        input_schema=_object_schema(
            {
                **REPOSITORY_PROPERTIES,
                "state": {"enum": ["open", "closed", "all"], "default": "open"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            ["owner", "repository"],
        ),
        output_schema=_object_schema(
            {
                "repository": {"type": "string"},
                "issues": {"type": "array", "maxItems": 100, "items": _ISSUE_OUTPUT},
                "truncated": {"type": "boolean"},
            },
            ["repository", "issues", "truncated"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("github.repository", ("owner", "repository")),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={
                "operation": "github.issue.list",
                "input": {"owner": "octo", "repository": "demo", "limit": 20},
            },
            normalized_errors=_COMMON_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="github.pull_request.list",
        provider_id="github",
        title="List pull requests",
        description="List a bounded set of normalized pull requests from one approved repository.",
        input_schema=_object_schema(
            {
                **REPOSITORY_PROPERTIES,
                "state": {"enum": ["open", "closed", "all"], "default": "open"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            ["owner", "repository"],
        ),
        output_schema=_object_schema(
            {
                "repository": {"type": "string"},
                "pull_requests": {"type": "array", "maxItems": 100, "items": _PULL_REQUEST_OUTPUT},
                "truncated": {"type": "boolean"},
            },
            ["repository", "pull_requests", "truncated"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("github.repository", ("owner", "repository")),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={
                "operation": "github.pull_request.list",
                "input": {"owner": "octo", "repository": "demo", "limit": 20},
            },
            normalized_errors=_COMMON_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="github.repository.trending.list",
        provider_id="github",
        title="List trending repositories",
        description="Read GitHub's actual daily, weekly, or monthly Trending repository order and enrich each selected repository with a bounded README.",
        input_schema=_object_schema(
            {
                "language": {
                    "type": ["string", "null"],
                    "maxLength": 100,
                    "pattern": "^[A-Za-z0-9+#._-]+$",
                    "default": None,
                },
                "period": {"type": "string", "enum": ["daily", "weekly", "monthly"], "default": "weekly"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            [],
        ),
        output_schema=_object_schema(
            {
                "ranking": {"const": "github_trending"},
                "period": {"enum": ["daily", "weekly", "monthly"]},
                "language": {"type": ["string", "null"]},
                "repositories": {"type": "array", "maxItems": 25, "items": _TRENDING_REPOSITORY_OUTPUT},
                "truncated": {"type": "boolean"},
            },
            ["ranking", "period", "language", "repositories", "truncated"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("github.provider", ()),
        risk=RiskLevel.LOW,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={
                "operation": "github.repository.trending.list",
                "input": {"period": "weekly", "language": "python", "limit": 10},
            },
            normalized_errors=_COMMON_ERRORS,
            open_world=True,
        ),
    ),
)
_ATLAS_ERRORS = (
    "connection_unavailable",
    "atlas_locked",
    "operation_undeclared",
    "authorization_missing_or_stale",
    "invalid_input",
    "not_found",
    "node_already_known",
    "stale_revision",
    "codex_unavailable",
    "codex_failed",
    "provider_timeout",
    "response_too_large",
    "provider_unavailable",
    "internal_failure",
)
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}
_KEYWORDS = {"type": "string", "minLength": 1, "maxLength": 200}
_NULLABLE_TEXT = {"type": ["string", "null"]}
_KNOWLEDGE_STATUS = {"enum": ["unassessed", "unknown", "known"]}
_KNOWLEDGE_BRANCH = {"enum": ["subjects", "ideologies"]}
_KNOWLEDGE_SUMMARY = _object_schema(
    {
        "node_id": {"type": "integer", "minimum": 1},
        "name": {"type": "string"},
        "path": {"type": "array", "items": {"type": "string"}},
        "status": _KNOWLEDGE_STATUS,
    },
    ["node_id", "name", "path", "status"],
)
_ATLAS_OPERATIONS = (
    IntegrationOperationSpec(
        id="atlas.person.get",
        provider_id="atlas",
        title="Get Atlas person",
        description="Read the non-sensitive built-in Person profile from the unlocked local Atlas.",
        input_schema=_object_schema({}, []),
        output_schema=_object_schema({"personal_info": {"type": ["object", "null"]}}, ["personal_info"]),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.person", ()),
        risk=RiskLevel.LOW,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.person.get", "input": {}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.experience.list",
        provider_id="atlas",
        title="List Atlas experiences",
        description="List recent experiences, optionally filtered by keywords and ongoing state.",
        input_schema=_object_schema({"keywords": _KEYWORDS, "ongoing": {"type": "boolean"}, "limit": _LIMIT}, []),
        output_schema=_object_schema(
            {"experiences": {"type": "array", "maxItems": 100, "items": {"type": "object"}}}, ["experiences"]
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.experience", ()),
        risk=RiskLevel.LOW,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.experience.list", "input": {"keywords": "research", "limit": 10}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.goal.list",
        provider_id="atlas",
        title="List Atlas goals",
        description="List matching top-level goals while retaining each complete subgoal tree and progression edges.",
        input_schema=_object_schema(
            {
                "importance": {"enum": ["low", "medium", "high"]},
                "horizon": {"enum": ["short", "middle", "long"]},
                "limit": _LIMIT,
            },
            [],
        ),
        output_schema=_object_schema(
            {
                "goals": {"type": "array", "maxItems": 100, "items": {"type": "object"}},
                "progressions": {"type": "array", "items": {"type": "object"}},
            },
            ["goals", "progressions"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.goal", ()),
        risk=RiskLevel.LOW,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.goal.list", "input": {"importance": "high", "horizon": "long"}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.project.list",
        provider_id="atlas",
        title="List Atlas projects",
        description="List projects with title, description, status, and GitHub link using minimal optional filters.",
        input_schema=_object_schema(
            {
                "keywords": _KEYWORDS,
                "status": {"enum": ["planned", "active", "paused", "completed", "abandoned"]},
                "has_github_link": {"type": "boolean"},
                "limit": _LIMIT,
            },
            [],
        ),
        output_schema=_object_schema(
            {"projects": {"type": "array", "maxItems": 100, "items": {"type": "object"}}}, ["projects"]
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.project", ()),
        risk=RiskLevel.LOW,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.project.list", "input": {"status": "active", "has_github_link": True}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.relationship.list",
        provider_id="atlas",
        title="List Atlas relationships",
        description="List important built-in relationship fields using optional keyword, type, status, and importance filters.",
        input_schema=_object_schema(
            {
                "keywords": _KEYWORDS,
                "kind": {"enum": ["family", "partner", "friend", "acquaintance", "coworker", "mentor", "org"]},
                "status": {"enum": ["active", "dormant", "past"]},
                "importance": {"enum": ["low", "medium", "high"]},
                "limit": _LIMIT,
            },
            [],
        ),
        output_schema=_object_schema(
            {"relationships": {"type": "array", "maxItems": 100, "items": {"type": "object"}}}, ["relationships"]
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.relationship", ()),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.relationship.list", "input": {"kind": "friend", "importance": "high"}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.knowledge.frontier.list",
        provider_id="atlas",
        title="Get Knowledge frontier",
        description="List bounded Subject nodes ready for assessment: unknown or unassessed nodes with a known immediate parent.",
        input_schema=_object_schema(
            {
                "root_node_id": {"type": "integer", "minimum": 1},
                "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            [],
        ),
        output_schema=_object_schema(
            {
                "nodes": {"type": "array", "maxItems": 100, "items": _KNOWLEDGE_SUMMARY},
                "next_cursor": {"type": ["string", "null"], "maxLength": 512},
            },
            ["nodes", "next_cursor"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.knowledge", ()),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.knowledge.frontier.list", "input": {"limit": 50}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.knowledge.search",
        provider_id="atlas",
        title="Search Knowledge nodes",
        description="Search Knowledge names, then known explanations and terms, and return compact ranked candidates.",
        input_schema=_object_schema(
            {
                "keywords": _KEYWORDS,
                "root_node_id": {"type": "integer", "minimum": 1},
                "branch": _KNOWLEDGE_BRANCH,
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
            },
            ["keywords"],
        ),
        output_schema=_object_schema(
            {"nodes": {"type": "array", "maxItems": 25, "items": _KNOWLEDGE_SUMMARY}}, ["nodes"]
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.knowledge", ()),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.knowledge.search", "input": {"keywords": "distributed systems"}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.knowledge.node.get",
        provider_id="atlas",
        title="Get Knowledge node",
        description="Inspect one Knowledge node with only its path, parent, immediate children, explanation, terms, status, and revision.",
        input_schema=_object_schema({"node_id": {"type": "integer", "minimum": 1}}, ["node_id"]),
        output_schema=_object_schema({"node": {"type": "object"}}, ["node"]),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("atlas.knowledge.node", ("node_id",)),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.knowledge.node.get", "input": {"node_id": 42}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="atlas.knowledge.node.know",
        provider_id="atlas",
        title="Know Knowledge node",
        description="Use one internet-enabled Codex call, then primitive Atlas writes, to mark the selected node known and optionally create immediate name-only unassessed children. It cannot rename, move, delete, merge, or recursively expand nodes.",
        input_schema=_object_schema(
            {
                "node_id": {"type": "integer", "minimum": 1},
                "explanation": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            ["node_id"],
        ),
        output_schema=_object_schema(
            {
                "node": {"type": "object"},
                "created_children": {"type": "array", "items": {"type": "string"}},
                "existing_children": {"type": "array", "items": {"type": "string"}},
            },
            ["node", "created_children", "existing_children"],
        ),
        effects=frozenset({IntegrationEffect.UPDATE, IntegrationEffect.EXECUTE}),
        resource=ResourceSpec("atlas.knowledge.node", ("node_id",)),
        risk=RiskLevel.MEDIUM,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "atlas.knowledge.node.know", "input": {"node_id": 42}},
            normalized_errors=_ATLAS_ERRORS,
            open_world=False,
        ),
    ),
)
_NOTION_ERRORS = (
    "connection_unavailable",
    "invalid_credential",
    "operation_undeclared",
    "authorization_missing_or_stale",
    "invalid_input",
    "not_found",
    "schema_mismatch",
    "provider_forbidden",
    "rate_limited",
    "provider_timeout",
    "response_too_large",
    "provider_unavailable",
    "internal_failure",
)
_TODO_DATE = {
    "type": ["string", "null"],
    "pattern": "^\\d{4}-\\d{2}-\\d{2}(?:T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?(?:Z|[+-]\\d{2}:\\d{2}))?$",
}
_TODO_PRIORITY = {"type": ["string", "null"], "enum": ["low", "medium", "high", None]}
_TODO_OPTIONAL_TEXT = {"type": ["string", "null"], "maxLength": 2000}
_TODO_OUTPUT = _object_schema(
    {
        "id": {"type": "string", "minLength": 1, "maxLength": 128},
        "title": {"type": "string", "minLength": 1, "maxLength": 2000},
        "done": {"type": "boolean"},
        "priority": _TODO_PRIORITY,
        "start_at": _TODO_DATE,
        "due_at": _TODO_DATE,
        "estimated_minutes": {"type": ["integer", "null"], "minimum": 1},
        "atlas_goal_id": _TODO_OPTIONAL_TEXT,
        "notes": _TODO_OPTIONAL_TEXT,
        "created_at": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    [
        "id",
        "title",
        "done",
        "priority",
        "start_at",
        "due_at",
        "estimated_minutes",
        "atlas_goal_id",
        "notes",
        "created_at",
    ],
)
_TODO_MUTABLE_PROPERTIES = {
    "title": {"type": "string", "minLength": 1, "maxLength": 2000},
    "done": {"type": "boolean"},
    "priority": _TODO_PRIORITY,
    "start_at": _TODO_DATE,
    "due_at": _TODO_DATE,
    "estimated_minutes": {"type": ["integer", "null"], "minimum": 1},
    "atlas_goal_id": _TODO_OPTIONAL_TEXT,
    "notes": _TODO_OPTIONAL_TEXT,
}
_TODO_UPDATE_INPUT = _object_schema(
    {"id": {"type": "string", "minLength": 1, "maxLength": 128}, **_TODO_MUTABLE_PROPERTIES}, ["id"]
)
_TODO_UPDATE_INPUT["minProperties"] = 2
_REPORT_SELECT = {"type": "string", "enum": ["GitHub Projects", "AI News", "AI Research", "Macro", "Personal Feed"]}
_REPORT_OUTPUT = _object_schema(
    {
        "id": {"type": "string", "minLength": 1, "maxLength": 128},
        "name": {"type": "string", "minLength": 1, "maxLength": 2000},
        "created_time": {"type": "string", "minLength": 1, "maxLength": 64},
        "select": _REPORT_SELECT,
    },
    ["id", "name", "created_time", "select"],
)
_REPORT_PAGE_INPUT_PROPERTIES = {
    "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    "start_cursor": {"type": "string", "minLength": 1, "maxLength": 2048},
}
_NOTION_BLOCKS = {"type": "array", "maxItems": 100, "items": {"type": "object"}}
_NOTION_OPERATIONS = (
    IntegrationOperationSpec(
        id="notion.todo.list",
        provider_id="notion",
        title="List Notion todos",
        description="List one bounded page of todos from the configured Notion data source, newest-created first.",
        input_schema=_object_schema(
            {
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
                "start_cursor": {"type": "string", "minLength": 1, "maxLength": 2048},
            },
            [],
        ),
        output_schema=_object_schema(
            {
                "todos": {"type": "array", "maxItems": 100, "items": _TODO_OUTPUT},
                "has_more": {"type": "boolean"},
                "next_cursor": {"type": ["string", "null"], "maxLength": 2048},
            },
            ["todos", "has_more", "next_cursor"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("notion.todo", ()),
        risk=RiskLevel.LOW,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "notion.todo.list", "input": {"page_size": 25}},
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="notion.todo.create",
        provider_id="notion",
        title="Create Notion todo",
        description="Create one todo in the configured Notion data source; only title is required.",
        input_schema=_object_schema(dict(_TODO_MUTABLE_PROPERTIES), ["title"]),
        output_schema=_TODO_OUTPUT,
        effects=frozenset({IntegrationEffect.CREATE}),
        resource=ResourceSpec("notion.todo", ()),
        risk=RiskLevel.MEDIUM,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "notion.todo.create", "input": {"title": "Buy groceries"}},
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="notion.todo.update",
        provider_id="notion",
        title="Update Notion todo",
        description="Partially update one contained Notion todo; explicit null clears an optional property.",
        input_schema=_TODO_UPDATE_INPUT,
        output_schema=_TODO_OUTPUT,
        effects=frozenset({IntegrationEffect.UPDATE}),
        resource=ResourceSpec("notion.todo", ("id",)),
        risk=RiskLevel.MEDIUM,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "notion.todo.update", "input": {"id": "page-id", "done": True}},
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="notion.todo.delete",
        provider_id="notion",
        title="Delete Notion todo",
        description="Move one contained Notion todo page to trash; Notion does not support permanent API deletion.",
        input_schema=_object_schema({"id": {"type": "string", "minLength": 1, "maxLength": 128}}, ["id"]),
        output_schema=_object_schema(
            {"id": {"type": "string", "minLength": 1, "maxLength": 128}, "removed": {"type": "boolean", "const": True}},
            ["id", "removed"],
        ),
        effects=frozenset({IntegrationEffect.DELETE}),
        resource=ResourceSpec("notion.todo", ("id",)),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={"operation": "notion.todo.delete", "input": {"id": "page-id"}},
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="notion.report.list",
        provider_id="notion",
        title="List Notion reports",
        description="List one bounded page of reports from the configured Notion Reports data source, newest-created first.",
        input_schema=_object_schema(dict(_REPORT_PAGE_INPUT_PROPERTIES), []),
        output_schema=_object_schema(
            {
                "reports": {"type": "array", "maxItems": 100, "items": _REPORT_OUTPUT},
                "has_more": {"type": "boolean"},
                "next_cursor": {"type": ["string", "null"], "maxLength": 2048},
            },
            ["reports", "has_more", "next_cursor"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("notion.report", ()),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "notion.report.list", "input": {"page_size": 25}},
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="notion.report.get",
        provider_id="notion",
        title="Get Notion report",
        description="Get one contained Notion report and one raw paginated page of its top-level blocks.",
        input_schema=_object_schema(
            {"id": {"type": "string", "minLength": 1, "maxLength": 128}, **_REPORT_PAGE_INPUT_PROPERTIES}, ["id"]
        ),
        output_schema=_object_schema(
            {
                "report": _REPORT_OUTPUT,
                "blocks": _NOTION_BLOCKS,
                "has_more": {"type": "boolean"},
                "next_cursor": {"type": ["string", "null"], "maxLength": 2048},
            },
            ["report", "blocks", "has_more", "next_cursor"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("notion.report", ("id",)),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "notion.report.get", "input": {"id": "page-id"}},
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="notion.report.create",
        provider_id="notion",
        title="Create Notion report",
        description="Create one report with raw Notion children in the configured Notion Reports data source.",
        input_schema=_object_schema(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 2000},
                "select": _REPORT_SELECT,
                "children": _NOTION_BLOCKS,
            },
            ["name", "select", "children"],
        ),
        output_schema=_REPORT_OUTPUT,
        effects=frozenset({IntegrationEffect.CREATE}),
        resource=ResourceSpec("notion.report", ()),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={
                "operation": "notion.report.create",
                "input": {"name": "Weekly report", "select": "GitHub Projects", "children": []},
            },
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
    IntegrationOperationSpec(
        id="notion.report.delete",
        provider_id="notion",
        title="Delete Notion report",
        description="Move one contained Notion report page to trash; Notion does not support permanent API deletion.",
        input_schema=_object_schema({"id": {"type": "string", "minLength": 1, "maxLength": 128}}, ["id"]),
        output_schema=_object_schema(
            {"id": {"type": "string", "minLength": 1, "maxLength": 128}, "removed": {"type": "boolean", "const": True}},
            ["id", "removed"],
        ),
        effects=frozenset({IntegrationEffect.DELETE}),
        resource=ResourceSpec("notion.report", ("id",)),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={"operation": "notion.report.delete", "input": {"id": "page-id"}},
            normalized_errors=_NOTION_ERRORS,
            open_world=True,
        ),
    ),
)
_GOOGLE_CALENDAR_ERRORS = (
    "connection_unavailable",
    "invalid_credential",
    "operation_undeclared",
    "authorization_missing_or_stale",
    "invalid_input",
    "not_found",
    "provider_forbidden",
    "rate_limited",
    "provider_timeout",
    "response_too_large",
    "provider_unavailable",
    "internal_failure",
)
_GOOGLE_EVENT_ID = {"type": "string", "minLength": 1, "maxLength": 1024}
_GOOGLE_EVENT_TIME_INPUT = {
    "oneOf": [
        _object_schema(
            {
                "date_time": {"type": "string", "minLength": 1, "maxLength": 64},
                "time_zone": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            ["date_time", "time_zone"],
        ),
        _object_schema({"date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}}, ["date"]),
    ]
}
_GOOGLE_EVENT_TIME_OUTPUT = {
    "oneOf": [
        _object_schema(
            {
                "date_time": {"type": "string", "minLength": 1, "maxLength": 64},
                "time_zone": {"type": ["string", "null"], "maxLength": 64},
            },
            ["date_time", "time_zone"],
        ),
        _object_schema({"date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}}, ["date"]),
    ]
}
_GOOGLE_RECURRENCE = {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 512}}
_GOOGLE_EVENT_OUTPUT = _object_schema(
    {
        "id": _GOOGLE_EVENT_ID,
        "status": {"type": "string", "enum": ["confirmed", "tentative"]},
        "summary": {"type": ["string", "null"], "maxLength": 1024},
        "description": {"type": ["string", "null"], "maxLength": 8192},
        "location": {"type": ["string", "null"], "maxLength": 1024},
        "start": _GOOGLE_EVENT_TIME_OUTPUT,
        "end": _GOOGLE_EVENT_TIME_OUTPUT,
        "recurrence": _GOOGLE_RECURRENCE,
        "recurring_event_id": {"type": ["string", "null"], "maxLength": 1024},
        "original_start": {"oneOf": [_GOOGLE_EVENT_TIME_OUTPUT, {"type": "null"}]},
        "html_link": {"type": ["string", "null"], "maxLength": 4096},
        "created_at": {"type": "string", "minLength": 1, "maxLength": 64},
        "updated_at": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    [
        "id",
        "status",
        "summary",
        "description",
        "location",
        "start",
        "end",
        "recurrence",
        "recurring_event_id",
        "original_start",
        "html_link",
        "created_at",
        "updated_at",
    ],
)
_GOOGLE_MUTABLE_PROPERTIES = {
    "title": {"type": "string", "minLength": 1, "maxLength": 1024},
    "description": {"type": ["string", "null"], "maxLength": 8192},
    "location": {"type": ["string", "null"], "maxLength": 1024},
    "start": _GOOGLE_EVENT_TIME_INPUT,
    "end": _GOOGLE_EVENT_TIME_INPUT,
    "recurrence": _GOOGLE_RECURRENCE,
}
_GOOGLE_CREATE_PROPERTIES = {**_GOOGLE_MUTABLE_PROPERTIES, "recurrence": {**_GOOGLE_RECURRENCE, "minItems": 1}}
_GOOGLE_UPDATE_INPUT = _object_schema({"id": _GOOGLE_EVENT_ID, **_GOOGLE_MUTABLE_PROPERTIES}, ["id"])
_GOOGLE_UPDATE_INPUT["minProperties"] = 2
_GOOGLE_CALENDAR_OPERATIONS = (
    IntegrationOperationSpec(
        id="google_calendar.event.create",
        provider_id="google_calendar",
        title="Create Google Calendar event",
        description="Create one timed, all-day, or recurring event on the authenticated user's primary calendar.",
        input_schema=_object_schema(dict(_GOOGLE_CREATE_PROPERTIES), ["title", "start", "end"]),
        output_schema=_GOOGLE_EVENT_OUTPUT,
        effects=frozenset({IntegrationEffect.CREATE}),
        resource=ResourceSpec("calendar.event", ()),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={
                "operation": "google_calendar.event.create",
                "input": {
                    "title": "Planning",
                    "start": {"date_time": "2026-09-01T09:00:00-04:00", "time_zone": "America/Toronto"},
                    "end": {"date_time": "2026-09-01T10:00:00-04:00", "time_zone": "America/Toronto"},
                },
            },
            normalized_errors=_GOOGLE_CALENDAR_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="google_calendar.event.list",
        provider_id="google_calendar",
        title="List Google Calendar events",
        description="List one bounded page of upcoming primary-calendar event instances in start-time order.",
        input_schema=_object_schema(
            {
                "time_min": {"type": "string", "minLength": 1, "maxLength": 64},
                "time_max": {"type": "string", "minLength": 1, "maxLength": 64},
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
                "page_token": {"type": "string", "minLength": 1, "maxLength": 2048},
            },
            [],
        ),
        output_schema=_object_schema(
            {
                "events": {"type": "array", "maxItems": 100, "items": _GOOGLE_EVENT_OUTPUT},
                "time_min": {"type": "string", "minLength": 1, "maxLength": 64},
                "time_max": {"type": ["string", "null"], "maxLength": 64},
                "has_more": {"type": "boolean"},
                "next_page_token": {"type": ["string", "null"], "maxLength": 2048},
            },
            ["events", "time_min", "time_max", "has_more", "next_page_token"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("calendar.event", ()),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "google_calendar.event.list", "input": {"page_size": 25}},
            normalized_errors=_GOOGLE_CALENDAR_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="google_calendar.event.get",
        provider_id="google_calendar",
        title="Get Google Calendar event",
        description="Get one exact event or recurring-series master from the primary calendar.",
        input_schema=_object_schema({"id": _GOOGLE_EVENT_ID}, ["id"]),
        output_schema=_GOOGLE_EVENT_OUTPUT,
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("calendar.event", ("id",)),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "google_calendar.event.get", "input": {"id": "event-id"}},
            normalized_errors=_GOOGLE_CALENDAR_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="google_calendar.event.update",
        provider_id="google_calendar",
        title="Update Google Calendar event",
        description="Partially update exactly one primary-calendar event instance or recurring-series master.",
        input_schema=_GOOGLE_UPDATE_INPUT,
        output_schema=_GOOGLE_EVENT_OUTPUT,
        effects=frozenset({IntegrationEffect.UPDATE}),
        resource=ResourceSpec("calendar.event", ("id",)),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={
                "operation": "google_calendar.event.update",
                "input": {"id": "event-id", "location": "Room 2"},
            },
            normalized_errors=_GOOGLE_CALENDAR_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="google_calendar.event.delete",
        provider_id="google_calendar",
        title="Delete Google Calendar event",
        description="Delete exactly one primary-calendar event instance or recurring-series master.",
        input_schema=_object_schema({"id": _GOOGLE_EVENT_ID}, ["id"]),
        output_schema=_object_schema(
            {"id": _GOOGLE_EVENT_ID, "deleted": {"type": "boolean", "const": True}}, ["id", "deleted"]
        ),
        effects=frozenset({IntegrationEffect.DELETE}),
        resource=ResourceSpec("calendar.event", ("id",)),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={"operation": "google_calendar.event.delete", "input": {"id": "event-id"}},
            normalized_errors=_GOOGLE_CALENDAR_ERRORS,
            open_world=False,
        ),
    ),
)
_EMAIL_ADDRESS = {"type": "string", "minLength": 3, "maxLength": 320, "pattern": "^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$"}
_EMAIL_ADDRESS_LIST = {"type": "array", "maxItems": 10, "uniqueItems": True, "items": _EMAIL_ADDRESS}
_EMAIL_ATTACHMENT = _object_schema(
    {
        "filename": {"type": "string", "maxLength": 1024},
        "mime_type": {"type": "string", "maxLength": 255},
        "size": {"type": "integer", "minimum": 0},
    },
    ["filename", "mime_type", "size"],
)
_EMAIL_MESSAGE = _object_schema(
    {
        "message_id": {"type": "string", "minLength": 1, "maxLength": 1024},
        "conversation_id": {"type": "string", "minLength": 1, "maxLength": 1024},
        "from": {"type": "string", "maxLength": 2000},
        "to": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 2000}},
        "cc": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 2000}},
        "bcc": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 2000}},
        "subject": {"type": "string", "maxLength": 2000},
        "date": {"type": "string", "maxLength": 128},
        "snippet": {"type": "string", "maxLength": 2000},
        "text": {"type": "string", "maxLength": 4000000},
        "unread": {"type": "boolean"},
        "attachments": {"type": "array", "maxItems": 100, "items": _EMAIL_ATTACHMENT},
    },
    [
        "message_id",
        "conversation_id",
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "date",
        "snippet",
        "text",
        "unread",
        "attachments",
    ],
)
_EMAIL_CONVERSATION_SUMMARY = _object_schema(
    {
        "conversation_id": {"type": "string", "minLength": 1, "maxLength": 1024},
        "subject": {"type": "string", "maxLength": 2000},
        "latest_sender": {"type": "string", "maxLength": 2000},
        "latest_date": {"type": "string", "maxLength": 128},
        "snippet": {"type": "string", "maxLength": 2000},
        "message_count": {"type": "integer", "minimum": 1, "maximum": 100},
        "unread": {"type": "boolean"},
        "has_attachment": {"type": "boolean"},
    },
    [
        "conversation_id",
        "subject",
        "latest_sender",
        "latest_date",
        "snippet",
        "message_count",
        "unread",
        "has_attachment",
    ],
)
_EMAIL_SEARCH_INPUT = _object_schema(
    {
        "keywords": {"type": "string", "minLength": 1, "maxLength": 500},
        "from": _EMAIL_ADDRESS,
        "to": _EMAIL_ADDRESS,
        "subject": {"type": "string", "minLength": 1, "maxLength": 500},
        "after": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "before": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "unread": {"type": "boolean"},
        "has_attachment": {"type": "boolean"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 25, "default": 25},
        "page_token": {"type": "string", "minLength": 1, "maxLength": 2048},
    },
    [],
)
_EMAIL_SEARCH_INPUT["anyOf"] = [
    {"required": [field]}
    for field in ("keywords", "from", "to", "subject", "after", "before", "unread", "has_attachment")
]
_GMAIL_ERRORS = (
    "connection_unavailable",
    "invalid_credential",
    "operation_undeclared",
    "authorization_missing_or_stale",
    "invalid_input",
    "not_found",
    "provider_forbidden",
    "rate_limited",
    "provider_timeout",
    "response_too_large",
    "provider_unavailable",
    "internal_failure",
)
_GMAIL_OPERATIONS = (
    IntegrationOperationSpec(
        id="email.search",
        provider_id="gmail",
        title="Search email",
        description="Search Gmail with provider-neutral keywords and filters and return bounded conversation summaries.",
        input_schema=_EMAIL_SEARCH_INPUT,
        output_schema=_object_schema(
            {
                "conversations": {"type": "array", "maxItems": 25, "items": _EMAIL_CONVERSATION_SUMMARY},
                "has_more": {"type": "boolean"},
                "next_page_token": {"type": ["string", "null"], "maxLength": 2048},
            },
            ["conversations", "has_more", "next_page_token"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("gmail.conversation", ()),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "email.search", "input": {"keywords": "quarterly report", "page_size": 10}},
            normalized_errors=_GMAIL_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="email.conversation.get",
        provider_id="gmail",
        title="Get email conversation",
        description="Read one complete bounded email conversation as normalized text with attachment metadata.",
        input_schema=_object_schema(
            {"conversation_id": {"type": "string", "minLength": 1, "maxLength": 1024}}, ["conversation_id"]
        ),
        output_schema=_object_schema(
            {
                "conversation_id": {"type": "string", "minLength": 1, "maxLength": 1024},
                "messages": {"type": "array", "maxItems": 100, "items": _EMAIL_MESSAGE},
            },
            ["conversation_id", "messages"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("gmail.conversation", ("conversation_id",)),
        risk=RiskLevel.LOW,
        presentation=OperationPresentation(
            usage_example={"operation": "email.conversation.get", "input": {"conversation_id": "thread-id"}},
            normalized_errors=_GMAIL_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="email.read_new",
        provider_id="gmail",
        title="Read new email",
        description="Read up to 50 unread Primary Inbox messages from the last year without changing their read state.",
        input_schema=_object_schema({}, []),
        output_schema=_object_schema(
            {
                "messages": {"type": "array", "maxItems": 50, "items": _EMAIL_MESSAGE},
                "count": {"type": "integer", "minimum": 0, "maximum": 50},
                "has_more": {"type": "boolean"},
            },
            ["messages", "count", "has_more"],
        ),
        effects=frozenset({IntegrationEffect.READ}),
        resource=ResourceSpec("gmail.message", ()),
        risk=RiskLevel.LOW,
        contract_version=2,
        presentation=OperationPresentation(
            usage_example={"operation": "email.read_new", "input": {}},
            normalized_errors=_GMAIL_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="email.read_and_mark_new",
        provider_id="gmail",
        title="Read and mark new email",
        description="Read up to 50 unread Primary Inbox messages from the last year, then mark exactly that fetched batch read.",
        input_schema=_object_schema({}, []),
        output_schema=_object_schema(
            {
                "messages": {"type": "array", "maxItems": 50, "items": _EMAIL_MESSAGE},
                "count": {"type": "integer", "minimum": 0, "maximum": 50},
                "has_more": {"type": "boolean"},
            },
            ["messages", "count", "has_more"],
        ),
        effects=frozenset({IntegrationEffect.READ, IntegrationEffect.UPDATE}),
        resource=ResourceSpec("gmail.message", ()),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={"operation": "email.read_and_mark_new", "input": {}},
            normalized_errors=_GMAIL_ERRORS,
            open_world=False,
        ),
    ),
    IntegrationOperationSpec(
        id="email.send",
        provider_id="gmail",
        title="Send email",
        description="Send one bounded plain-text email from the authenticated Gmail account.",
        input_schema=_object_schema(
            {
                "to": {**_EMAIL_ADDRESS_LIST, "minItems": 1},
                "cc": _EMAIL_ADDRESS_LIST,
                "bcc": _EMAIL_ADDRESS_LIST,
                "subject": {"type": "string", "minLength": 1, "maxLength": 500},
                "body": {"type": "string", "minLength": 1, "maxLength": 20000},
            },
            ["to", "subject", "body"],
        ),
        output_schema=_object_schema(
            {
                "sent": {"type": "boolean", "const": True},
                "message_id": {"type": "string", "minLength": 1, "maxLength": 1024},
                "conversation_id": {"type": "string", "minLength": 1, "maxLength": 1024},
            },
            ["sent", "message_id", "conversation_id"],
        ),
        effects=frozenset({IntegrationEffect.SEND}),
        resource=ResourceSpec("gmail.message", ()),
        risk=RiskLevel.HIGH,
        presentation=OperationPresentation(
            usage_example={
                "operation": "email.send",
                "input": {"to": ["person@example.com"], "subject": "Hello", "body": "Hello from Eidolon."},
            },
            normalized_errors=_GMAIL_ERRORS,
            open_world=False,
        ),
    ),
)
_TELEGRAM_ERRORS = (
    "connection_unavailable",
    "invalid_credential",
    "operation_undeclared",
    "authorization_missing_or_stale",
    "invalid_input",
    "provider_forbidden",
    "rate_limited",
    "provider_timeout",
    "response_too_large",
    "provider_unavailable",
    "internal_failure",
)
_TELEGRAM_OPERATIONS = (
    IntegrationOperationSpec(
        id="telegram.notification.send",
        provider_id="telegram",
        title="Send Telegram notification",
        description="Send one structured notification to the paired Eidolon Telegram chat.",
        input_schema=_object_schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "description": {"type": "string", "minLength": 1, "maxLength": 800},
                "link": {"type": "string", "minLength": 1, "maxLength": 2048, "pattern": "^https?://"},
                "alert": {"type": "boolean"},
            },
            ["title", "description"],
        ),
        output_schema=_object_schema(
            {"sent": {"type": "boolean", "const": True}, "message_id": {"type": "integer", "minimum": 1}},
            ["sent", "message_id"],
        ),
        effects=frozenset({IntegrationEffect.SEND}),
        resource=ResourceSpec("telegram.message", ()),
        risk=RiskLevel.MEDIUM,
        presentation=OperationPresentation(
            usage_example={
                "operation": "telegram.notification.send",
                "input": {"title": "Build complete", "description": "The requested build finished.", "alert": False},
            },
            normalized_errors=_TELEGRAM_ERRORS,
            open_world=False,
        ),
    ),
)

_PROVIDER_SPECS = (
    ProviderSpec(id="github", display_name="GitHub"),
    ProviderSpec(id="atlas", display_name="Atlas"),
    ProviderSpec(id="notion", display_name="Notion"),
    ProviderSpec(id="google_calendar", display_name="Google Calendar"),
    ProviderSpec(id="gmail", display_name="Gmail"),
    ProviderSpec(id="telegram", display_name="Telegram"),
)
_STABLE_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_STABLE_OPERATION_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_STABLE_RESOURCE_TYPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_STABLE_IDENTITY_FIELD = re.compile(r"^[a-z][a-z0-9_]*$")


class IntegrationOperationRegistry:
    def __init__(self, providers: Iterable[ProviderSpec], operations: Iterable[IntegrationOperationSpec]) -> None:
        provider_items, operation_items = tuple(providers), tuple(operations)
        self._validate_providers(provider_items)
        self._validate_operations(operation_items, {provider.id for provider in provider_items})
        self._providers = MappingProxyType(
            {provider.id: provider for provider in sorted(provider_items, key=lambda item: item.id)}
        )
        self._operations = MappingProxyType(
            {operation.id: operation for operation in sorted(operation_items, key=lambda item: item.id)}
        )

    @staticmethod
    def _validate_providers(providers: tuple[ProviderSpec, ...]) -> None:
        seen: set[str] = set()
        for provider in providers:
            if not isinstance(provider, ProviderSpec):
                raise ValueError("Integration providers must be ProviderSpec instances")
            if provider.id in seen:
                raise ValueError(f"Duplicate integration provider ID: {provider.id}")
            seen.add(provider.id)
            if not _STABLE_PROVIDER_ID.fullmatch(provider.id):
                raise ValueError(f"Invalid integration provider ID: {provider.id}")
            if not provider.display_name.strip():
                raise ValueError(f"Integration provider {provider.id} has no display name")

    @classmethod
    def _validate_operations(cls, operations: tuple[IntegrationOperationSpec, ...], provider_ids: set[str]) -> None:
        seen: set[str] = set()
        for operation in operations:
            if not isinstance(operation, IntegrationOperationSpec):
                raise ValueError("Integration operations must be IntegrationOperationSpec instances")
            if operation.id in seen:
                raise ValueError(f"Duplicate integration operation ID: {operation.id}")
            seen.add(operation.id)
            if not _STABLE_OPERATION_ID.fullmatch(operation.id):
                raise ValueError(f"Invalid integration operation ID: {operation.id}")
            if operation.provider_id not in provider_ids:
                raise ValueError(
                    f"Integration operation {operation.id} references missing provider {operation.provider_id}"
                )
            if not operation.id.startswith(f"{operation.provider_id}.") and not (
                operation.provider_id == "gmail" and operation.id.startswith("email.")
            ):
                raise ValueError(f"Integration operation {operation.id} does not use its stable provider namespace")
            if not operation.title.strip() or not operation.description.strip():
                raise ValueError(f"Integration operation {operation.id} has incomplete metadata")
            if not isinstance(operation.effects, frozenset) or not operation.effects:
                raise ValueError(f"Integration operation {operation.id} must declare canonical effects")
            if any(not isinstance(effect, IntegrationEffect) for effect in operation.effects):
                raise ValueError(f"Integration operation {operation.id} has an invalid effect")
            if not isinstance(operation.risk, RiskLevel):
                raise ValueError(f"Integration operation {operation.id} has an invalid risk")
            cls._validate_resource(operation.id, operation.resource)
            if (
                not isinstance(operation.contract_version, int)
                or isinstance(operation.contract_version, bool)
                or operation.contract_version < 1
            ):
                raise ValueError(f"Integration operation {operation.id} has an invalid contract version")
            cls._validate_schema(operation.id, "input", operation.input_schema)
            cls._validate_schema(operation.id, "output", operation.output_schema)
            presentation = operation.presentation
            if presentation is not None:
                if not isinstance(presentation, OperationPresentation):
                    raise ValueError(f"Integration operation {operation.id} has invalid presentation metadata")
                if not isinstance(presentation.usage_example, dict):
                    raise ValueError(f"Integration operation {operation.id} has an invalid usage example")
                if not isinstance(presentation.normalized_errors, tuple) or any(
                    not isinstance(error, str) or not error for error in presentation.normalized_errors
                ):
                    raise ValueError(f"Integration operation {operation.id} has invalid normalized errors")
                if not isinstance(presentation.open_world, bool):
                    raise ValueError(f"Integration operation {operation.id} has invalid open-world metadata")

    @staticmethod
    def _validate_resource(operation_id: str, resource: ResourceSpec) -> None:
        if not isinstance(resource, ResourceSpec):
            raise ValueError(f"Integration operation {operation_id} has an invalid resource")
        if not _STABLE_RESOURCE_TYPE.fullmatch(resource.type):
            raise ValueError(f"Integration operation {operation_id} has an invalid resource type")
        if not isinstance(resource.identity_fields, tuple):
            raise ValueError(f"Integration operation {operation_id} resource identity fields must be a tuple")
        if len(set(resource.identity_fields)) != len(resource.identity_fields) or any(
            not isinstance(field, str) or not _STABLE_IDENTITY_FIELD.fullmatch(field)
            for field in resource.identity_fields
        ):
            raise ValueError(f"Integration operation {operation_id} has invalid resource identity fields")

    @staticmethod
    def _validate_schema(operation_id: str, schema_kind: str, schema: dict[str, Any]) -> None:
        if not isinstance(schema, dict):
            raise ValueError(f"Integration operation {operation_id} {schema_kind} schema must be an object")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"Integration operation {operation_id} has an invalid {schema_kind} schema") from exc

    def get(self, operation_id: str) -> IntegrationOperationSpec | None:
        return self._operations.get(operation_id)

    def list(self) -> tuple[IntegrationOperationSpec, ...]:
        return tuple(self._operations.values())

    def for_provider(self, provider_id: str) -> tuple[IntegrationOperationSpec, ...]:
        return tuple(operation for operation in self._operations.values() if operation.provider_id == provider_id)

    def providers(self) -> tuple[ProviderSpec, ...]:
        return tuple(self._providers.values())

    @property
    def operation_mapping(self) -> MappingProxyType[str, IntegrationOperationSpec]:
        return self._operations


DEFAULT_INTEGRATION_REGISTRY = IntegrationOperationRegistry(
    _PROVIDER_SPECS,
    (
        *_GITHUB_OPERATIONS,
        *_ATLAS_OPERATIONS,
        *_NOTION_OPERATIONS,
        *_GOOGLE_CALENDAR_OPERATIONS,
        *_GMAIL_OPERATIONS,
        *_TELEGRAM_OPERATIONS,
    ),
)


def registry_contract_identity(operation_ids: list[str]) -> dict[str, dict[str, Any]]:
    return {
        operation_id: DEFAULT_INTEGRATION_REGISTRY.get(operation_id).contract_identity()
        for operation_id in sorted(operation_ids)
    }
