from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

ScopeBehavior = Literal["repository", "none"]

REPOSITORY_PROPERTIES = {
    "owner": {"type": "string", "pattern": r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"},
    "repository": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]{1,100}$"},
}


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class IntegrationOperation:
    operation_id: str
    title: str
    description: str
    provider: Literal["github", "atlas", "notion", "google_calendar", "gmail", "telegram"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_only: bool
    side_effect: Literal["none", "write"]
    risk: Literal["low", "medium"]
    resource_scope: ScopeBehavior
    method: Literal["GET", "POST", "PATCH", "DELETE"]
    endpoint_template: str
    timeout_seconds: float
    allow_redirects: bool
    max_pages: int
    max_results: int
    max_provider_response_bytes: int
    normalized_errors: tuple[str, ...]
    audit_resource_fields: tuple[str, ...]
    fake_behavior: str
    usage_example: dict[str, Any]
    requires_invocation_approval: bool = False
    contract_version: int = 1

    def agent_context(self) -> dict[str, Any]:
        return {
            "operation": self.operation_id,
            "title": self.title,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "resource_scope": self.resource_scope,
            "read_only": self.read_only,
            "risk": self.risk,
            "requires_invocation_approval": self.requires_invocation_approval,
            "normalized_errors": list(self.normalized_errors),
            "usage_example": self.usage_example,
            "helper": (
                "integration_runtime_capabilities.call(operation=..., input=...) for function or service code; "
                "web_runtime_capabilities.call_integration(operation=..., input=...) for web-app server code"
            ),
            "test_adapter": (
                "In tests, use integration_test_adapter.DeterministicFakeIntegrationAdapter and monkeypatch the "
                "runtime helper call; use registry-shaped deterministic responses and failures; never use a real "
                "credential or live provider request."
            ),
        }


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
    ["full_name", "description", "private", "default_branch", "html_url", "stars", "forks", "open_issues", "updated_at"],
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

_OPERATIONS = (
    IntegrationOperation(
        operation_id="github.repository.get",
        title="Get repository",
        description="Read normalized metadata for one approved GitHub repository.",
        provider="github",
        input_schema=_object_schema(dict(REPOSITORY_PROPERTIES), ["owner", "repository"]),
        output_schema=_REPOSITORY_OUTPUT,
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="repository",
        method="GET",
        endpoint_template="/repos/{owner}/{repository}",
        timeout_seconds=10,
        allow_redirects=False,
        max_pages=1,
        max_results=1,
        max_provider_response_bytes=1_000_000,
        normalized_errors=_COMMON_ERRORS,
        audit_resource_fields=("owner", "repository"),
        fake_behavior="repository_metadata",
        usage_example={"operation": "github.repository.get", "input": {"owner": "octo", "repository": "demo"}},
    ),
    IntegrationOperation(
        operation_id="github.repository.tree.list",
        title="List repository tree",
        description="List a bounded, depth-limited repository tree and report truncation.",
        provider="github",
        input_schema=_object_schema(
            {
                **REPOSITORY_PROPERTIES,
                "ref": {"type": "string", "minLength": 1, "maxLength": 200, "default": "HEAD"},
                "path": {
                    "type": "string",
                    "maxLength": 500,
                    "pattern": r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$)).*$",
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
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="repository",
        method="GET",
        endpoint_template="/repos/{owner}/{repository}/git/trees/{ref}?recursive=1",
        timeout_seconds=15,
        allow_redirects=False,
        max_pages=1,
        max_results=500,
        max_provider_response_bytes=5_000_000,
        normalized_errors=_COMMON_ERRORS,
        audit_resource_fields=("owner", "repository"),
        fake_behavior="repository_tree",
        usage_example={
            "operation": "github.repository.tree.list",
            "input": {"owner": "octo", "repository": "demo", "depth": 2},
        },
    ),
    IntegrationOperation(
        operation_id="github.repository.file.read",
        title="Read repository file",
        description="Read one bounded UTF-8 text file from an approved repository.",
        provider="github",
        input_schema=_object_schema(
            {
                **REPOSITORY_PROPERTIES,
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "pattern": r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+$",
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
                "text": {"type": "string", "maxLength": 262_144},
                "size": {"type": "integer", "minimum": 0, "maximum": 262_144},
                "sha": {"type": "string"},
            },
            ["repository", "path", "ref", "text", "size", "sha"],
        ),
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="repository",
        method="GET",
        endpoint_template="/repos/{owner}/{repository}/contents/{path}?ref={ref}",
        timeout_seconds=10,
        allow_redirects=False,
        max_pages=1,
        max_results=1,
        max_provider_response_bytes=1_000_000,
        normalized_errors=_COMMON_ERRORS + ("unsupported_file_type",),
        audit_resource_fields=("owner", "repository"),
        fake_behavior="repository_file",
        usage_example={
            "operation": "github.repository.file.read",
            "input": {"owner": "octo", "repository": "demo", "path": "README.md"},
        },
    ),
    IntegrationOperation(
        operation_id="github.issue.list",
        title="List issues",
        description="List a bounded set of normalized issues from one approved repository.",
        provider="github",
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
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="repository",
        method="GET",
        endpoint_template="/repos/{owner}/{repository}/issues",
        timeout_seconds=10,
        allow_redirects=False,
        max_pages=1,
        max_results=100,
        max_provider_response_bytes=3_000_000,
        normalized_errors=_COMMON_ERRORS,
        audit_resource_fields=("owner", "repository"),
        fake_behavior="issue_list",
        usage_example={"operation": "github.issue.list", "input": {"owner": "octo", "repository": "demo", "limit": 20}},
    ),
    IntegrationOperation(
        operation_id="github.pull_request.list",
        title="List pull requests",
        description="List a bounded set of normalized pull requests from one approved repository.",
        provider="github",
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
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="repository",
        method="GET",
        endpoint_template="/repos/{owner}/{repository}/pulls",
        timeout_seconds=10,
        allow_redirects=False,
        max_pages=1,
        max_results=100,
        max_provider_response_bytes=3_000_000,
        normalized_errors=_COMMON_ERRORS,
        audit_resource_fields=("owner", "repository"),
        fake_behavior="pull_request_list",
        usage_example={
            "operation": "github.pull_request.list",
            "input": {"owner": "octo", "repository": "demo", "limit": 20},
        },
    ),
    IntegrationOperation(
        operation_id="github.repository.trending.list",
        title="List trending repositories",
        description=(
            "Read GitHub's actual daily, weekly, or monthly Trending repository order and enrich each selected "
            "repository with a bounded README."
        ),
        provider="github",
        input_schema=_object_schema(
            {
                "language": {
                    "type": ["string", "null"],
                    "maxLength": 100,
                    "pattern": r"^[A-Za-z0-9+#._-]+$",
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
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="none",
        method="GET",
        endpoint_template="https://github.com/trending[/{language}]?since={period}; /repos/{owner}/{repository}/readme",
        timeout_seconds=15,
        allow_redirects=False,
        max_pages=1,
        max_results=25,
        max_provider_response_bytes=5_000_000,
        normalized_errors=_COMMON_ERRORS,
        audit_resource_fields=(),
        fake_behavior="trending_repositories",
        usage_example={
            "operation": "github.repository.trending.list",
            "input": {"period": "weekly", "language": "python", "limit": 10},
        },
        contract_version=2,
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
    IntegrationOperation(
        operation_id="atlas.person.get",
        title="Get Atlas person",
        description="Read the non-sensitive built-in Person profile from the unlocked local Atlas.",
        provider="atlas",
        input_schema=_object_schema({}, []),
        output_schema=_object_schema({"personal_info": {"type": ["object", "null"]}}, ["personal_info"]),
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="none",
        method="GET",
        endpoint_template="/api/records?category=person",
        timeout_seconds=10,
        allow_redirects=False,
        max_pages=1,
        max_results=1,
        max_provider_response_bytes=1_000_000,
        normalized_errors=_ATLAS_ERRORS,
        audit_resource_fields=(),
        fake_behavior="atlas_person",
        usage_example={"operation": "atlas.person.get", "input": {}},
        contract_version=2,
    ),
    IntegrationOperation(
        operation_id="atlas.experience.list",
        title="List Atlas experiences",
        description="List recent experiences, optionally filtered by keywords and ongoing state.",
        provider="atlas",
        input_schema=_object_schema(
            {"keywords": _KEYWORDS, "ongoing": {"type": "boolean"}, "limit": _LIMIT}, []
        ),
        output_schema=_object_schema(
            {"experiences": {"type": "array", "maxItems": 100, "items": {"type": "object"}}},
            ["experiences"],
        ),
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/api/records?category=experience", timeout_seconds=10, allow_redirects=False,
        max_pages=1, max_results=100, max_provider_response_bytes=2_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=(), fake_behavior="atlas_experiences",
        usage_example={"operation": "atlas.experience.list", "input": {"keywords": "research", "limit": 10}},
        contract_version=2,
    ),
    IntegrationOperation(
        operation_id="atlas.goal.list",
        title="List Atlas goals",
        description="List matching top-level goals while retaining each complete subgoal tree and progression edges.",
        provider="atlas",
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
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/api/records?category=goal", timeout_seconds=10, allow_redirects=False,
        max_pages=1, max_results=100, max_provider_response_bytes=3_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=(), fake_behavior="atlas_goals",
        usage_example={"operation": "atlas.goal.list", "input": {"importance": "high", "horizon": "long"}},
        contract_version=2,
    ),
    IntegrationOperation(
        operation_id="atlas.project.list",
        title="List Atlas projects",
        description="List projects with title, description, status, and GitHub link using minimal optional filters.",
        provider="atlas",
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
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/api/records?category=project", timeout_seconds=10, allow_redirects=False,
        max_pages=1, max_results=100, max_provider_response_bytes=2_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=(), fake_behavior="atlas_projects",
        usage_example={"operation": "atlas.project.list", "input": {"status": "active", "has_github_link": True}},
        contract_version=2,
    ),
    IntegrationOperation(
        operation_id="atlas.relationship.list",
        title="List Atlas relationships",
        description="List important built-in relationship fields using optional keyword, type, status, and importance filters.",
        provider="atlas",
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
            {"relationships": {"type": "array", "maxItems": 100, "items": {"type": "object"}}},
            ["relationships"],
        ),
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/api/records?category=relationship", timeout_seconds=10, allow_redirects=False,
        max_pages=1, max_results=100, max_provider_response_bytes=3_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=(), fake_behavior="atlas_relationships",
        usage_example={"operation": "atlas.relationship.list", "input": {"kind": "friend", "importance": "high"}},
    ),
    IntegrationOperation(
        operation_id="atlas.knowledge.frontier.list",
        title="Get Knowledge frontier",
        description="List bounded Subject nodes ready for assessment: unknown or unassessed nodes with a known immediate parent.",
        provider="atlas",
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
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/api/knowledge/nodes", timeout_seconds=10, allow_redirects=False,
        max_pages=1, max_results=100, max_provider_response_bytes=2_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=(), fake_behavior="atlas_knowledge_frontier",
        usage_example={"operation": "atlas.knowledge.frontier.list", "input": {"limit": 50}},
    ),
    IntegrationOperation(
        operation_id="atlas.knowledge.search",
        title="Search Knowledge nodes",
        description="Search Knowledge names, then known explanations and terms, and return compact ranked candidates.",
        provider="atlas",
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
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/api/knowledge/nodes", timeout_seconds=10, allow_redirects=False,
        max_pages=1, max_results=25, max_provider_response_bytes=1_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=(), fake_behavior="atlas_knowledge_search",
        usage_example={"operation": "atlas.knowledge.search", "input": {"keywords": "distributed systems"}},
    ),
    IntegrationOperation(
        operation_id="atlas.knowledge.node.get",
        title="Get Knowledge node",
        description="Inspect one Knowledge node with only its path, parent, immediate children, explanation, terms, status, and revision.",
        provider="atlas",
        input_schema=_object_schema({"node_id": {"type": "integer", "minimum": 1}}, ["node_id"]),
        output_schema=_object_schema({"node": {"type": "object"}}, ["node"]),
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/api/knowledge/nodes", timeout_seconds=10, allow_redirects=False,
        max_pages=1, max_results=1, max_provider_response_bytes=1_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=("node_id",), fake_behavior="atlas_knowledge_node",
        usage_example={"operation": "atlas.knowledge.node.get", "input": {"node_id": 42}},
    ),
    IntegrationOperation(
        operation_id="atlas.knowledge.node.know",
        title="Know Knowledge node",
        description=(
            "Use one internet-enabled Codex call, then primitive Atlas writes, to mark the selected node known and "
            "optionally create immediate name-only unassessed children. It cannot rename, move, delete, merge, or "
            "recursively expand nodes."
        ),
        provider="atlas",
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
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="PATCH",
        endpoint_template="/api/knowledge/nodes/{node_id}", timeout_seconds=180, allow_redirects=False,
        max_pages=1, max_results=21, max_provider_response_bytes=2_000_000,
        normalized_errors=_ATLAS_ERRORS, audit_resource_fields=("node_id",), fake_behavior="atlas_knowledge_know",
        usage_example={"operation": "atlas.knowledge.node.know", "input": {"node_id": 42}},
        contract_version=2,
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
    "pattern": r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?$",
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
        "id", "title", "done", "priority", "start_at", "due_at", "estimated_minutes",
        "atlas_goal_id", "notes", "created_at",
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
    {"id": {"type": "string", "minLength": 1, "maxLength": 128}, **_TODO_MUTABLE_PROPERTIES},
    ["id"],
)
_TODO_UPDATE_INPUT["minProperties"] = 2

_REPORT_SELECT = {
    "type": "string",
    "enum": ["GitHub Projects", "AI News", "AI Research", "Macro", "Personal Feed"],
}
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
_NOTION_BLOCKS = {
    "type": "array",
    "maxItems": 100,
    "items": {"type": "object"},
}

_NOTION_OPERATIONS = (
    IntegrationOperation(
        operation_id="notion.todo.list",
        title="List Notion todos",
        description="List one bounded page of todos from the configured Notion data source, newest-created first.",
        provider="notion",
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
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="POST",
        endpoint_template="/v1/data_sources/{configured_data_source_id}/query",
        timeout_seconds=15, allow_redirects=False, max_pages=1, max_results=100,
        max_provider_response_bytes=2_000_000, normalized_errors=_NOTION_ERRORS,
        audit_resource_fields=(), fake_behavior="notion_todo_list",
        usage_example={"operation": "notion.todo.list", "input": {"page_size": 25}},
        contract_version=2,
    ),
    IntegrationOperation(
        operation_id="notion.todo.create",
        title="Create Notion todo",
        description="Create one todo in the configured Notion data source; only title is required.",
        provider="notion",
        input_schema=_object_schema(dict(_TODO_MUTABLE_PROPERTIES), ["title"]),
        output_schema=_TODO_OUTPUT,
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="POST",
        endpoint_template="/v1/pages (fixed configured data-source parent)", timeout_seconds=15,
        allow_redirects=False, max_pages=1, max_results=1, max_provider_response_bytes=2_000_000,
        normalized_errors=_NOTION_ERRORS, audit_resource_fields=(), fake_behavior="notion_todo_create",
        usage_example={"operation": "notion.todo.create", "input": {"title": "Buy groceries"}},
        contract_version=2,
    ),
    IntegrationOperation(
        operation_id="notion.todo.update",
        title="Update Notion todo",
        description="Partially update one contained Notion todo; explicit null clears an optional property.",
        provider="notion",
        input_schema=_TODO_UPDATE_INPUT,
        output_schema=_TODO_OUTPUT,
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="PATCH",
        endpoint_template="/v1/pages/{id} after configured data-source containment check", timeout_seconds=15,
        allow_redirects=False, max_pages=1, max_results=1, max_provider_response_bytes=2_000_000,
        normalized_errors=_NOTION_ERRORS, audit_resource_fields=("id",), fake_behavior="notion_todo_update",
        usage_example={"operation": "notion.todo.update", "input": {"id": "page-id", "done": True}},
        contract_version=2,
    ),
    IntegrationOperation(
        operation_id="notion.todo.delete",
        title="Delete Notion todo",
        description="Move one contained Notion todo page to trash; Notion does not support permanent API deletion.",
        provider="notion",
        input_schema=_object_schema({"id": {"type": "string", "minLength": 1, "maxLength": 128}}, ["id"]),
        output_schema=_object_schema(
            {
                "id": {"type": "string", "minLength": 1, "maxLength": 128},
                "removed": {"type": "boolean", "const": True},
            },
            ["id", "removed"],
        ),
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="PATCH",
        endpoint_template="/v1/pages/{id} with in_trash=true after configured data-source containment check",
        timeout_seconds=15, allow_redirects=False, max_pages=1, max_results=1,
        max_provider_response_bytes=2_000_000, normalized_errors=_NOTION_ERRORS,
        audit_resource_fields=("id",), fake_behavior="notion_todo_delete",
        usage_example={"operation": "notion.todo.delete", "input": {"id": "page-id"}},
    ),
    IntegrationOperation(
        operation_id="notion.report.list",
        title="List Notion reports",
        description="List one bounded page of reports from the configured Notion Reports data source, newest-created first.",
        provider="notion",
        input_schema=_object_schema(dict(_REPORT_PAGE_INPUT_PROPERTIES), []),
        output_schema=_object_schema(
            {
                "reports": {"type": "array", "maxItems": 100, "items": _REPORT_OUTPUT},
                "has_more": {"type": "boolean"},
                "next_cursor": {"type": ["string", "null"], "maxLength": 2048},
            },
            ["reports", "has_more", "next_cursor"],
        ),
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="POST",
        endpoint_template="/v1/data_sources/{configured_report_data_source_id}/query",
        timeout_seconds=15, allow_redirects=False, max_pages=1, max_results=100,
        max_provider_response_bytes=2_000_000, normalized_errors=_NOTION_ERRORS,
        audit_resource_fields=(), fake_behavior="notion_report_list",
        usage_example={"operation": "notion.report.list", "input": {"page_size": 25}},
    ),
    IntegrationOperation(
        operation_id="notion.report.get",
        title="Get Notion report",
        description="Get one contained Notion report and one raw paginated page of its top-level blocks.",
        provider="notion",
        input_schema=_object_schema(
            {
                "id": {"type": "string", "minLength": 1, "maxLength": 128},
                **_REPORT_PAGE_INPUT_PROPERTIES,
            },
            ["id"],
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
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/v1/pages/{id} plus /v1/blocks/{id}/children after configured Reports containment check",
        timeout_seconds=15, allow_redirects=False, max_pages=1, max_results=100,
        max_provider_response_bytes=2_000_000, normalized_errors=_NOTION_ERRORS,
        audit_resource_fields=("id",), fake_behavior="notion_report_get",
        usage_example={"operation": "notion.report.get", "input": {"id": "page-id"}},
    ),
    IntegrationOperation(
        operation_id="notion.report.create",
        title="Create Notion report",
        description="Create one report with raw Notion children in the configured Notion Reports data source.",
        provider="notion",
        input_schema=_object_schema(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 2000},
                "select": _REPORT_SELECT,
                "children": _NOTION_BLOCKS,
            },
            ["name", "select", "children"],
        ),
        output_schema=_REPORT_OUTPUT,
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="POST",
        endpoint_template="/v1/pages with fixed configured Reports data-source parent",
        timeout_seconds=15, allow_redirects=False, max_pages=1, max_results=1,
        max_provider_response_bytes=2_000_000, normalized_errors=_NOTION_ERRORS,
        audit_resource_fields=(), fake_behavior="notion_report_create",
        usage_example={
            "operation": "notion.report.create",
            "input": {"name": "Weekly report", "select": "GitHub Projects", "children": []},
        },
    ),
    IntegrationOperation(
        operation_id="notion.report.delete",
        title="Delete Notion report",
        description="Move one contained Notion report page to trash; Notion does not support permanent API deletion.",
        provider="notion",
        input_schema=_object_schema(
            {"id": {"type": "string", "minLength": 1, "maxLength": 128}},
            ["id"],
        ),
        output_schema=_object_schema(
            {
                "id": {"type": "string", "minLength": 1, "maxLength": 128},
                "removed": {"type": "boolean", "const": True},
            },
            ["id", "removed"],
        ),
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="PATCH",
        endpoint_template="/v1/pages/{id} with in_trash=true after configured Reports containment check",
        timeout_seconds=15, allow_redirects=False, max_pages=1, max_results=1,
        max_provider_response_bytes=2_000_000, normalized_errors=_NOTION_ERRORS,
        audit_resource_fields=("id",), fake_behavior="notion_report_delete",
        usage_example={"operation": "notion.report.delete", "input": {"id": "page-id"}},
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
        _object_schema(
            {"date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}},
            ["date"],
        ),
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
        _object_schema(
            {"date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}},
            ["date"],
        ),
    ]
}
_GOOGLE_RECURRENCE = {
    "type": "array",
    "maxItems": 20,
    "items": {"type": "string", "minLength": 1, "maxLength": 512},
}
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
        "id", "status", "summary", "description", "location", "start", "end", "recurrence",
        "recurring_event_id", "original_start", "html_link", "created_at", "updated_at",
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
_GOOGLE_CREATE_PROPERTIES = {
    **_GOOGLE_MUTABLE_PROPERTIES,
    "recurrence": {**_GOOGLE_RECURRENCE, "minItems": 1},
}
_GOOGLE_UPDATE_INPUT = _object_schema(
    {"id": _GOOGLE_EVENT_ID, **_GOOGLE_MUTABLE_PROPERTIES},
    ["id"],
)
_GOOGLE_UPDATE_INPUT["minProperties"] = 2

_GOOGLE_CALENDAR_OPERATIONS = (
    IntegrationOperation(
        operation_id="google_calendar.event.create",
        title="Create Google Calendar event",
        description="Create one timed, all-day, or recurring event on the authenticated user's primary calendar.",
        provider="google_calendar",
        input_schema=_object_schema(dict(_GOOGLE_CREATE_PROPERTIES), ["title", "start", "end"]),
        output_schema=_GOOGLE_EVENT_OUTPUT,
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="POST",
        endpoint_template="/calendar/v3/calendars/primary/events", timeout_seconds=15,
        allow_redirects=False, max_pages=1, max_results=1, max_provider_response_bytes=2_000_000,
        normalized_errors=_GOOGLE_CALENDAR_ERRORS, audit_resource_fields=(),
        fake_behavior="google_calendar_event_create",
        usage_example={
            "operation": "google_calendar.event.create",
            "input": {
                "title": "Planning",
                "start": {"date_time": "2026-09-01T09:00:00-04:00", "time_zone": "America/Toronto"},
                "end": {"date_time": "2026-09-01T10:00:00-04:00", "time_zone": "America/Toronto"},
            },
        },
    ),
    IntegrationOperation(
        operation_id="google_calendar.event.list",
        title="List Google Calendar events",
        description="List one bounded page of upcoming primary-calendar event instances in start-time order.",
        provider="google_calendar",
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
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/calendar/v3/calendars/primary/events", timeout_seconds=15,
        allow_redirects=False, max_pages=1, max_results=100, max_provider_response_bytes=2_000_000,
        normalized_errors=_GOOGLE_CALENDAR_ERRORS, audit_resource_fields=(),
        fake_behavior="google_calendar_event_list",
        usage_example={"operation": "google_calendar.event.list", "input": {"page_size": 25}},
    ),
    IntegrationOperation(
        operation_id="google_calendar.event.get",
        title="Get Google Calendar event",
        description="Get one exact event or recurring-series master from the primary calendar.",
        provider="google_calendar",
        input_schema=_object_schema({"id": _GOOGLE_EVENT_ID}, ["id"]),
        output_schema=_GOOGLE_EVENT_OUTPUT,
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/calendar/v3/calendars/primary/events/{id}", timeout_seconds=15,
        allow_redirects=False, max_pages=1, max_results=1, max_provider_response_bytes=2_000_000,
        normalized_errors=_GOOGLE_CALENDAR_ERRORS, audit_resource_fields=("id",),
        fake_behavior="google_calendar_event_get",
        usage_example={"operation": "google_calendar.event.get", "input": {"id": "event-id"}},
    ),
    IntegrationOperation(
        operation_id="google_calendar.event.update",
        title="Update Google Calendar event",
        description="Partially update exactly one primary-calendar event instance or recurring-series master.",
        provider="google_calendar",
        input_schema=_GOOGLE_UPDATE_INPUT,
        output_schema=_GOOGLE_EVENT_OUTPUT,
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="PATCH",
        endpoint_template="/calendar/v3/calendars/primary/events/{id}", timeout_seconds=15,
        allow_redirects=False, max_pages=1, max_results=1, max_provider_response_bytes=2_000_000,
        normalized_errors=_GOOGLE_CALENDAR_ERRORS, audit_resource_fields=("id",),
        fake_behavior="google_calendar_event_update",
        usage_example={
            "operation": "google_calendar.event.update",
            "input": {"id": "event-id", "location": "Room 2"},
        },
    ),
    IntegrationOperation(
        operation_id="google_calendar.event.delete",
        title="Delete Google Calendar event",
        description="Delete exactly one primary-calendar event instance or recurring-series master.",
        provider="google_calendar",
        input_schema=_object_schema({"id": _GOOGLE_EVENT_ID}, ["id"]),
        output_schema=_object_schema(
            {"id": _GOOGLE_EVENT_ID, "deleted": {"type": "boolean", "const": True}},
            ["id", "deleted"],
        ),
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="DELETE",
        endpoint_template="/calendar/v3/calendars/primary/events/{id}", timeout_seconds=15,
        allow_redirects=False, max_pages=1, max_results=1, max_provider_response_bytes=2_000_000,
        normalized_errors=_GOOGLE_CALENDAR_ERRORS, audit_resource_fields=("id",),
        fake_behavior="google_calendar_event_delete",
        usage_example={"operation": "google_calendar.event.delete", "input": {"id": "event-id"}},
    ),
)

_EMAIL_ADDRESS = {
    "type": "string",
    "minLength": 3,
    "maxLength": 320,
    "pattern": r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
}
_EMAIL_ADDRESS_LIST = {
    "type": "array",
    "maxItems": 10,
    "uniqueItems": True,
    "items": _EMAIL_ADDRESS,
}
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
        "text": {"type": "string", "maxLength": 4_000_000},
        "unread": {"type": "boolean"},
        "attachments": {"type": "array", "maxItems": 100, "items": _EMAIL_ATTACHMENT},
    },
    [
        "message_id", "conversation_id", "from", "to", "cc", "bcc", "subject", "date",
        "snippet", "text", "unread", "attachments",
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
        "conversation_id", "subject", "latest_sender", "latest_date", "snippet", "message_count",
        "unread", "has_attachment",
    ],
)
_EMAIL_SEARCH_INPUT = _object_schema(
    {
        "keywords": {"type": "string", "minLength": 1, "maxLength": 500},
        "from": _EMAIL_ADDRESS,
        "to": _EMAIL_ADDRESS,
        "subject": {"type": "string", "minLength": 1, "maxLength": 500},
        "after": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "before": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
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
    IntegrationOperation(
        operation_id="email.search",
        title="Search email",
        description="Search Gmail with provider-neutral keywords and filters and return bounded conversation summaries.",
        provider="gmail",
        input_schema=_EMAIL_SEARCH_INPUT,
        output_schema=_object_schema(
            {
                "conversations": {"type": "array", "maxItems": 25, "items": _EMAIL_CONVERSATION_SUMMARY},
                "has_more": {"type": "boolean"},
                "next_page_token": {"type": ["string", "null"], "maxLength": 2048},
            },
            ["conversations", "has_more", "next_page_token"],
        ),
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/gmail/v1/users/me/threads", timeout_seconds=20, allow_redirects=False,
        max_pages=1, max_results=25, max_provider_response_bytes=4_000_000,
        normalized_errors=_GMAIL_ERRORS, audit_resource_fields=(), fake_behavior="email_search",
        usage_example={"operation": "email.search", "input": {"keywords": "quarterly report", "page_size": 10}},
    ),
    IntegrationOperation(
        operation_id="email.conversation.get",
        title="Get email conversation",
        description="Read one complete bounded email conversation as normalized text with attachment metadata.",
        provider="gmail",
        input_schema=_object_schema(
            {"conversation_id": {"type": "string", "minLength": 1, "maxLength": 1024}},
            ["conversation_id"],
        ),
        output_schema=_object_schema(
            {
                "conversation_id": {"type": "string", "minLength": 1, "maxLength": 1024},
                "messages": {"type": "array", "maxItems": 100, "items": _EMAIL_MESSAGE},
            },
            ["conversation_id", "messages"],
        ),
        read_only=True, side_effect="none", risk="low", resource_scope="none", method="GET",
        endpoint_template="/gmail/v1/users/me/threads/{conversation_id}?format=full", timeout_seconds=20,
        allow_redirects=False, max_pages=1, max_results=100, max_provider_response_bytes=4_000_000,
        normalized_errors=_GMAIL_ERRORS, audit_resource_fields=("conversation_id",),
        fake_behavior="email_conversation_get",
        usage_example={"operation": "email.conversation.get", "input": {"conversation_id": "thread-id"}},
    ),
    IntegrationOperation(
        operation_id="email.read_new",
        title="Read new email",
        description=(
            "Read up to 50 unread Primary Inbox messages from the last year, then mark exactly that fetched batch read."
        ),
        provider="gmail",
        input_schema=_object_schema({}, []),
        output_schema=_object_schema(
            {
                "messages": {"type": "array", "maxItems": 50, "items": _EMAIL_MESSAGE},
                "count": {"type": "integer", "minimum": 0, "maximum": 50},
                "has_more": {"type": "boolean"},
            },
            ["messages", "count", "has_more"],
        ),
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="POST",
        endpoint_template="/gmail/v1/users/me/messages plus batchModify", timeout_seconds=30,
        allow_redirects=False, max_pages=1, max_results=50, max_provider_response_bytes=4_000_000,
        normalized_errors=_GMAIL_ERRORS, audit_resource_fields=(), fake_behavior="email_read_new",
        usage_example={"operation": "email.read_new", "input": {}},
    ),
    IntegrationOperation(
        operation_id="email.send",
        title="Send email",
        description="Send one bounded plain-text email from the authenticated Gmail account.",
        provider="gmail",
        input_schema=_object_schema(
            {
                "to": {**_EMAIL_ADDRESS_LIST, "minItems": 1},
                "cc": _EMAIL_ADDRESS_LIST,
                "bcc": _EMAIL_ADDRESS_LIST,
                "subject": {"type": "string", "minLength": 1, "maxLength": 500},
                "body": {"type": "string", "minLength": 1, "maxLength": 20_000},
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
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="POST",
        endpoint_template="/gmail/v1/users/me/messages/send", timeout_seconds=20, allow_redirects=False,
        max_pages=1, max_results=1, max_provider_response_bytes=2_000_000,
        normalized_errors=_GMAIL_ERRORS, audit_resource_fields=(), fake_behavior="email_send",
        usage_example={
            "operation": "email.send",
            "input": {"to": ["person@example.com"], "subject": "Hello", "body": "Hello from Eidolon."},
        },
        requires_invocation_approval=True,
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
    IntegrationOperation(
        operation_id="telegram.notification.send",
        title="Send Telegram notification",
        description="Send one structured notification to the paired Eidolon Telegram chat.",
        provider="telegram",
        input_schema=_object_schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "description": {"type": "string", "minLength": 1, "maxLength": 800},
                "link": {"type": "string", "minLength": 1, "maxLength": 2048, "pattern": r"^https?://"},
            },
            ["title", "description"],
        ),
        output_schema=_object_schema(
            {
                "sent": {"type": "boolean", "const": True},
                "message_id": {"type": "integer", "minimum": 1},
            },
            ["sent", "message_id"],
        ),
        read_only=False, side_effect="write", risk="medium", resource_scope="none", method="POST",
        endpoint_template="/bot<token>/sendMessage", timeout_seconds=15, allow_redirects=False,
        max_pages=1, max_results=1, max_provider_response_bytes=1_000_000,
        normalized_errors=_TELEGRAM_ERRORS, audit_resource_fields=(), fake_behavior="telegram_notification_send",
        usage_example={
            "operation": "telegram.notification.send",
            "input": {"title": "Build complete", "description": "The requested build finished."},
        },
    ),
)

OPERATIONS = MappingProxyType(
    {
        operation.operation_id: operation
        for operation in (
            *_OPERATIONS,
            *_ATLAS_OPERATIONS,
            *_NOTION_OPERATIONS,
            *_GOOGLE_CALENDAR_OPERATIONS,
            *_GMAIL_OPERATIONS,
            *_TELEGRAM_OPERATIONS,
        )
    }
)


def registry_contract_identity(operation_ids: list[str]) -> dict[str, dict[str, int | bool]]:
    return {
        operation_id: {
            "version": OPERATIONS[operation_id].contract_version,
            "requires_invocation_approval": OPERATIONS[
                operation_id
            ].requires_invocation_approval,
        }
        for operation_id in sorted(operation_ids)
    }
