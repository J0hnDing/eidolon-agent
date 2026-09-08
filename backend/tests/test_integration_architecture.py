from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Base
from app.execution.context import InvocationContext
from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.types import IntegrationEffect, IntegrationOperationSpec, RiskLevel

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _tree(relative_path: str) -> ast.Module:
    path = APP_ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _attribute_calls(node: ast.AST, attribute: str) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == attribute
    ]


def test_integration_handler_routes_catalog_calls_to_invocation_service() -> None:
    tree = _tree("execution/handlers/integration.py")
    handler = _class(tree, "IntegrationHandler")

    for method_name in ("execute", "execute_approved"):
        method = _method(handler, method_name)
        invocation_method = method_name
        invocation_calls = [
            call
            for call in _attribute_calls(method, invocation_method)
            if isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "invocations"
        ]
        assert invocation_calls, f"{method_name} must route through IntegrationInvocationService"

        # The compatibility IntegrationService remains available for settings and
        # connection management, but cannot be an execution/provider boundary.
        direct_service_calls = [
            call
            for call in ast.walk(method)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "service"
        ]
        assert not direct_service_calls


def test_invocation_service_authorizes_before_runtime_on_every_execution_path() -> None:
    tree = _tree("integrations/invocation.py")
    service = _class(tree, "IntegrationInvocationService")

    # Public dispatch selects one or more providers, then each helper owns the
    # policy/runtime boundary for its provider attempt.
    for method_name in ("_execute_single", "_execute_multi", "execute_approved"):
        method = _method(service, method_name)
        policy_calls = [
            call
            for call in _attribute_calls(method, "authorize")
            if isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "policy"
        ]
        runtime_calls = [
            call
            for call in _attribute_calls(method, "execute")
            if isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "runtime"
        ]
        assert len(policy_calls) == 1, f"{method_name} must call IntegrationCapabilityPolicy"
        assert runtime_calls, f"{method_name} must dispatch through IntegrationRuntime"
        assert min(call.lineno for call in policy_calls) < min(call.lineno for call in runtime_calls)


def test_runtime_is_the_only_catalog_provider_adapter_dispatcher() -> None:
    tree = _tree("integrations/runtime.py")
    runtime = _class(tree, "IntegrationRuntime")
    execute = _method(runtime, "execute")
    adapter_dispatches = [
        call
        for call in _attribute_calls(execute, "execute")
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "adapter"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "invocation"
    ]
    assert len(adapter_dispatches) == 1

    # Provider clients are still used by connection/OAuth control-plane code,
    # but the old IntegrationService execution switch must not call them.
    integration_service_tree = _tree("services/integration_service.py")
    provider_attrs = {
        "github",
        "atlas",
        "notion_provider_factory",
        "notion_report_provider_factory",
        "google_calendar",
        "gmail",
    }
    direct_provider_execution = [
        call
        for call in ast.walk(integration_service_tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "execute"
        and isinstance(call.func.value, ast.Attribute)
        and call.func.value.attr in provider_attrs
    ]
    assert not direct_provider_execution
    integration_service = _class(integration_service_tree, "IntegrationService")
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_invoke_operation", "invoke_operation"}
        for node in integration_service.body
    )


def test_production_consumers_do_not_use_legacy_operations_alias() -> None:
    compatibility_module = APP_ROOT / "services" / "integration_registry.py"
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        if path == compatibility_module:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_legacy = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.services.integration_registry"
            for node in ast.walk(tree)
        )
        uses_alias = any(
            isinstance(node, ast.Name) and node.id == "OPERATIONS"
            for node in ast.walk(tree)
        )
        if imports_legacy or uses_alias:
            offenders.append(path.relative_to(APP_ROOT).as_posix())
    assert offenders == []


def test_agent_integration_authorization_does_not_read_mcp_presentation_flags() -> None:
    tree = _tree("services/agent_policy_service.py")
    policy = _class(tree, "AgentPolicyService")
    method = _method(policy, "decision")
    integration_branches = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "operation"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], (ast.Is, ast.IsNot))
    ]
    assert integration_branches, "decision() must branch on the canonical integration operation"
    # MCP hints may remain as a compatibility projection for non-integration
    # entries, but must not occur in the canonical integration branch.
    assert not any(
        isinstance(constant, ast.Constant) and constant.value == "mcp_read_only"
        for branch in integration_branches
        for statement in branch.body
        for constant in ast.walk(statement)
    )


def test_canonical_operation_specs_have_no_independent_approval_or_transport_fields() -> None:
    field_names = {field.name.lower() for field in fields(IntegrationOperationSpec)}
    forbidden_fragments = (
        "approval",
        "http",
        "endpoint",
        "timeout",
        "pagination",
        "fake",
        "oauth",
        "credential",
        "token",
    )
    assert not {
        field_name
        for field_name in field_names
        if any(fragment in field_name for fragment in forbidden_fragments)
    }
    assert {
        "id",
        "title",
        "description",
        "input_schema",
        "output_schema",
        "effects",
        "resource",
        "risk",
        "contract_version",
        "presentation",
    }.issubset(field_names)


def test_approval_requirement_is_derived_only_from_canonical_risk() -> None:
    operations = DEFAULT_INTEGRATION_REGISTRY.list()
    assert operations
    assert {operation.risk for operation in operations} == {
        RiskLevel.LOW,
        RiskLevel.MEDIUM,
        RiskLevel.HIGH,
    }
    for operation in operations:
        required = operation.risk is RiskLevel.HIGH
        assert required is operation.requires_invocation_approval
        if operation.risk in {RiskLevel.LOW, RiskLevel.MEDIUM}:
            assert operation.requires_invocation_approval is False


def test_canonical_effects_classify_read_and_write_operations() -> None:
    write_effects = {
        IntegrationEffect.CREATE,
        IntegrationEffect.UPDATE,
        IntegrationEffect.DELETE,
        IntegrationEffect.SEND,
        IntegrationEffect.EXECUTE,
    }
    for operation in DEFAULT_INTEGRATION_REGISTRY.list():
        is_read_only = operation.effects == frozenset({IntegrationEffect.READ})
        assert operation.read_only is is_read_only
        if operation.effects & write_effects:
            assert not operation.read_only


def test_authorized_invocation_is_backend_issued_and_secret_free() -> None:
    operation = DEFAULT_INTEGRATION_REGISTRY.get("github.repository.get")
    assert operation is not None
    field_names = {field.name.lower() for field in fields(AuthorizedIntegrationInvocation)}
    assert not {
        name
        for name in field_names
        if any(fragment in name for fragment in ("secret", "token", "bearer", "credential"))
    }

    with pytest.raises(TypeError):
        AuthorizedIntegrationInvocation(  # type: ignore[call-arg]
            context=InvocationContext(principal_kind="user", origin="http"),
            operation=operation,
            input={"owner": "octo", "repository": "demo"},
            provider_id="github",
            provider_account_id="account-1",
            resource=None,
            effects=operation.effects,
            authorization_id=None,
        )


def test_high_risk_submission_records_security_metadata_and_does_not_dispatch(
    db_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This uses a connection-only compatibility double. No provider credential
    # is needed because policy must stop at the approval boundary.
    class TrackingRuntime:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def execute(self, invocation: object):  # noqa: ANN001, ANN201
            self.calls.append(invocation)
            raise AssertionError("provider runtime was reached before approval")

    class PairedTelegram:
        def approval_available(self) -> bool:
            return True

        def deliver_invocation_approval(self, _approval) -> None:  # noqa: ANN001
            return None

        def update_invocation_approval(self, _approval) -> None:  # noqa: ANN001
            return None

    compatibility = SimpleNamespace(
        db=db_session,
        project_root=tmp_path,
        secret_store=None,
        codex_adapter=None,
        operation_available=lambda _operation_id: True,
        provider_connected=lambda _provider_id: True,
        _connection=lambda provider_id: SimpleNamespace(
            account_id="gmail-account",
            provider=provider_id,
            status="connected",
        ),
    )
    monkeypatch.setattr(
        "app.services.invocation_approval_service.InvocationApprovalService._telegram_service",
        lambda _self: PairedTelegram(),
    )

    from app.integrations.invocation import IntegrationInvocationService
    from app.models import InvocationApproval

    runtime = TrackingRuntime()
    service = IntegrationInvocationService(
        db_session,
        compatibility_service=compatibility,
        project_root=tmp_path,
        runtime=runtime,
    )
    payload = {
        "provider": "gmail",
        "to": ["recipient@example.com"],
        "subject": "Approval boundary",
        "body": "This must wait for approval.",
        "reason_to_call": "User explicitly requested this message",
    }
    result = service.execute(
        InvocationContext(principal_kind="user", origin="http"),
        "email.send",
        payload,
    )

    assert result.output["status"] == "pending_approval"
    assert runtime.calls == []
    approval = db_session.get(InvocationApproval, result.output["approval_id"])
    assert approval is not None
    security = approval.dispatch_metadata_json["integration_security_v2"]
    assert set(security) == {
        "provider",
        "connection_id",
        "account_id",
        "risk",
        "effects",
        "resource",
    }
    assert security["provider"] == "gmail"
    assert security["connection_id"] is None
    assert security["account_id"] == "gmail-account"
    assert security["risk"] == "high"
    assert security["effects"] == ["send"]
    assert security["resource"] is None


def test_invocation_approval_atomic_claim_dispatches_at_most_once(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models import InvocationApproval
    from app.services.invocation_approval_service import InvocationApprovalService

    approval = InvocationApproval(
        target_kind="integration",
        target_id="email.send",
        target_contract_fingerprint="fingerprint",
        target_description="Send email",
        provider="gmail",
        provider_account_id="gmail-account",
        caller_type="local_user",
        source="direct_integration",
        input_json={},
        input_hash="input-hash",
        reason_to_call="User requested this message",
    )
    db_session.add(approval)
    db_session.commit()

    dispatches: list[int] = []
    service = InvocationApprovalService(db_session)

    def dispatch(claimed: InvocationApproval) -> InvocationApproval:
        dispatches.append(claimed.id)
        claimed.execution_status = "succeeded"
        claimed.result_json = {"sent": True}
        db_session.commit()
        return claimed

    monkeypatch.setattr(service, "_dispatch_claimed", dispatch)
    monkeypatch.setattr(service, "_update_telegram_outcome", lambda _approval: None)

    first = service.approve(approval.id, decided_via="local", decided_by="tester")
    second = service.approve(approval.id, decided_via="local", decided_by="tester")

    assert first.execution_status == "succeeded"
    assert second.execution_status == "succeeded"
    assert dispatches == [approval.id]


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
