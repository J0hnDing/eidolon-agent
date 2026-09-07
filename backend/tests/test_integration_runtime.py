from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.execution.context import InvocationContext
from app.integrations.authorization import (
    _issue_authorized_integration_invocation,
)
from app.integrations.providers import build_provider_adapters
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.runtime import (
    IntegrationRuntime,
    IntegrationRuntimeError,
    ProviderExecutionResult,
)
from app.integrations.types import IntegrationEffect, ResourceIdentity
from app.services.github_provider import IntegrationProviderError


class _Adapter:
    provider_id = "github"

    def __init__(self, result: ProviderExecutionResult) -> None:
        self.result = result
        self.calls = []

    def execute(self, invocation):  # noqa: ANN001, ANN201
        self.calls.append(invocation)
        return self.result


class _FailingAdapter:
    provider_id = "github"

    def execute(self, invocation):  # noqa: ANN001, ANN201
        del invocation
        raise IntegrationProviderError(
            "rate_limited", "credential=secret-sentinel", retry_after_seconds=12
        )


class _SecretStore:
    implementation_id = "fake-store"

    def get(self, reference: str, *, namespace: str) -> str:
        assert reference == "credential-reference"
        assert namespace == "github"
        return "credential-secret-sentinel"


class _GitHubTransport:
    def __init__(self) -> None:
        self.received_credential = None

    def execute(self, operation, input_json, credential):  # noqa: ANN001, ANN201
        del operation, input_json
        self.received_credential = credential
        return _repository_output()


class _CompatibilityService:
    def __init__(self) -> None:
        self.secret_store = _SecretStore()
        self.github = _GitHubTransport()
        self._github_connection = SimpleNamespace(
            secret_store_id="fake-store",
            secret_reference="credential-reference",
            status="connected",
            error_type=None,
        )

    def _connection(self, provider_id: str):  # noqa: ANN202
        assert provider_id == "github"
        return self._github_connection


def _invocation():
    operation = DEFAULT_INTEGRATION_REGISTRY.get("github.repository.get")
    assert operation is not None
    return _issue_authorized_integration_invocation(
        context=InvocationContext(principal_kind="user", origin="http"),
        operation=operation,
        input_json={"owner": "octo", "repository": "demo"},
        provider_account_id="account-1",
        resource=ResourceIdentity(
            type="github.repository", values={"owner": "octo", "repository": "demo"}
        ),
        authorization_id=None,
    )


def _repository_output() -> dict:
    return {
        "full_name": "octo/demo",
        "description": None,
        "private": False,
        "default_branch": "main",
        "html_url": "https://github.com/octo/demo",
        "stars": 1,
        "forks": 0,
        "open_issues": 0,
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_runtime_rejects_non_policy_invocation_before_adapter() -> None:
    adapter = _Adapter(ProviderExecutionResult(output=_repository_output()))
    runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=[adapter],
    )

    with pytest.raises(IntegrationRuntimeError) as exc_info:
        runtime.execute(SimpleNamespace(operation="github.repository.get"))

    assert exc_info.value.error_type == "authorization_missing_or_stale"
    assert adapter.calls == []


def test_runtime_propagates_authorized_output_and_audit_resource() -> None:
    expected = ProviderExecutionResult(
        output=_repository_output(), audit_resource="octo/demo"
    )
    adapter = _Adapter(expected)
    runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=[adapter],
    )

    assert runtime.execute(_invocation()) == expected
    assert adapter.calls == [_invocation()]


def test_runtime_rechecks_authorized_provider_and_effects() -> None:
    adapter = _Adapter(ProviderExecutionResult(output=_repository_output()))
    runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=[adapter],
    )
    inconsistent = replace(
        _invocation(), effects=frozenset({IntegrationEffect.UPDATE})
    )

    with pytest.raises(IntegrationRuntimeError) as exc_info:
        runtime.execute(inconsistent)

    assert exc_info.value.error_type == "authorization_missing_or_stale"
    assert adapter.calls == []


def test_runtime_normalizes_provider_error_without_raw_detail() -> None:
    runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=[_FailingAdapter()],
    )

    with pytest.raises(IntegrationRuntimeError) as exc_info:
        runtime.execute(_invocation())

    assert exc_info.value.error_type == "rate_limited"
    assert exc_info.value.retry_after == 12
    assert "secret-sentinel" not in str(exc_info.value)


def test_runtime_rejects_invalid_normalized_output() -> None:
    runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=[_Adapter(ProviderExecutionResult(output={"token": "secret-sentinel"}))],
    )

    with pytest.raises(IntegrationRuntimeError) as exc_info:
        runtime.execute(_invocation())

    assert exc_info.value.error_type == "internal_failure"
    assert "secret-sentinel" not in str(exc_info.value)


def test_default_adapter_composition_is_exact_and_secret_free() -> None:
    adapters = build_provider_adapters(SimpleNamespace())

    assert tuple(adapter.provider_id for adapter in adapters) == (
        "github",
        "atlas",
        "notion",
        "google_calendar",
        "gmail",
        "telegram",
    )
    invocation = _invocation()
    result = ProviderExecutionResult(output=_repository_output())
    assert "secret" not in repr(invocation).lower()
    assert "secret" not in repr(result).lower()


def test_provider_credential_stays_inside_adapter_execution() -> None:
    service = _CompatibilityService()
    runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=build_provider_adapters(service),
    )

    invocation = _invocation()
    result = runtime.execute(invocation)

    assert service.github.received_credential == "credential-secret-sentinel"
    assert result.audit_resource == "octo/demo"
    assert "credential-secret-sentinel" not in repr(invocation)
    assert "credential-secret-sentinel" not in repr(result)
