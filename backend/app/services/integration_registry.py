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
    provider: Literal["github"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_only: bool
    side_effect: Literal["none"]
    risk: Literal["low"]
    resource_scope: ScopeBehavior
    method: Literal["GET"]
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
            "usage_example": self.usage_example,
            "helper": (
                "integration_runtime_capabilities.call(operation=..., input=...) for function code; "
                "web_runtime_capabilities.call_integration(operation=..., input=...) for web-app server code"
            ),
            "test_adapter": (
                "In tests, use integration_test_adapter.DeterministicFakeIntegrationAdapter and monkeypatch the "
                "runtime helper call; never use a real token or live GitHub request."
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
            "Rank non-fork, non-archived public repositories created in the last 30 days by stars, then forks, "
            "then full name."
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
                "lookback_days": {"type": "integer", "enum": [30], "default": 30},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            [],
        ),
        output_schema=_object_schema(
            {
                "ranking": {"const": "stars_desc_forks_desc_full_name_asc"},
                "lookback_days": {"const": 30},
                "language": {"type": ["string", "null"]},
                "repositories": {"type": "array", "maxItems": 25, "items": _REPOSITORY_OUTPUT},
                "truncated": {"type": "boolean"},
            },
            ["ranking", "lookback_days", "language", "repositories", "truncated"],
        ),
        read_only=True,
        side_effect="none",
        risk="low",
        resource_scope="none",
        method="GET",
        endpoint_template=(
            "/search/repositories?q=created:>={lookback_start}+is:public+fork:false+archived:false"
            "[+language:{language}]&sort=stars&order=desc"
        ),
        timeout_seconds=15,
        allow_redirects=False,
        max_pages=1,
        max_results=25,
        max_provider_response_bytes=5_000_000,
        normalized_errors=_COMMON_ERRORS,
        audit_resource_fields=(),
        fake_behavior="trending_repositories",
        usage_example={"operation": "github.repository.trending.list", "input": {"language": "python", "limit": 10}},
    ),
)

OPERATIONS = MappingProxyType({operation.operation_id: operation for operation in _OPERATIONS})


def registry_contract_identity(operation_ids: list[str]) -> dict[str, int]:
    return {operation_id: OPERATIONS[operation_id].contract_version for operation_id in sorted(operation_ids)}
