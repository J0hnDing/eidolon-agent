from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.execution.context import InvocationContext
from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.policy import (
    IntegrationCapabilityPolicy,
    IntegrationPolicyError,
    PendingIntegrationApproval,
    integration_contract_fingerprint,
)
from app.integrations.registry import IntegrationOperationRegistry
from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    InvocationApprovalMode,
    ProviderSpec,
    ResourceSpec,
    RiskLevel,
)
from app.models import ActSession, AgentPolicy, IntegrationStandingGrant, InvocationApproval
from app.services.function_catalog_service import FunctionCatalogService
from app.services.integration_standing_grant_service import (
    IntegrationStandingGrantError,
    IntegrationStandingGrantService,
)
from app.services.invocation_approval_service import InvocationApprovalService


def _operation() -> IntegrationOperationSpec:
    return IntegrationOperationSpec(
        id="example.fixed.converse",
        title="Fixed conversation",
        description="Converse with a fixed integration recipient.",
        input_schema={
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effects=frozenset({IntegrationEffect.SEND}),
        resource=ResourceSpec("example.fixed"),
        risk=RiskLevel.HIGH,
        invocation_approval_mode=InvocationApprovalMode.STANDING_GRANT,
    )


def _registry(*operations) -> IntegrationOperationRegistry:
    return IntegrationOperationRegistry(
        [ProviderSpec("example", "Example", frozenset(op.id for op in operations))], operations,
    )


@pytest.fixture
def setup_policy(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        operation = _operation()
        registry = _registry(operation)
        monkeypatch.setattr("app.services.agent_policy_service.DEFAULT_INTEGRATION_REGISTRY", registry)
        entries = [{
            "id": operation.id, "category": "integration", "availability": "available",
            "mcp_exposed": True, "risk_level": "low", "mcp_read_only": True,
            "requires_invocation_approval": False,
        }]
        monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda _self: entries)
        contexts = {}
        for agent in ("assistant", "act", "observer"):
            session = ActSession(agent_id=agent, codex_thread_id=f"thread-{agent}")
            db.add(session)
            db.flush()
            contexts[agent] = InvocationContext(
                principal_kind="agent", origin="agent_mcp", agent_id=agent, agent_session_id=session.id,
            )
            if agent != "observer":
                db.add(AgentPolicy(id=agent, policy_json={"allowed_functions": [operation.id]}))
        db.commit()
        connection = SimpleNamespace(id=7, account_id="account-a")
        connections = SimpleNamespace(provider_connected=lambda _provider: True, connection=lambda _provider: connection)
        policy = IntegrationCapabilityPolicy(
            db, registry=registry, connection_service=connections, project_root=tmp_path,
        )
        bounds = {
            "operation_id": operation.id,
            "contract_fingerprint": policy.operation_contract_fingerprint(operation, "example"),
            "provider": "example", "connection_id": 7, "account_id": "account-a",
            "principal_kind": "agent", "principal_id": "assistant",
        }
        yield SimpleNamespace(
            db=db, policy=policy, operation=operation, registry=registry, contexts=contexts,
            grants=IntegrationStandingGrantService(db), bounds=bounds, connection=connection,
        )
    engine.dispose()


def _call(setup, context=None, **input_fields):
    return setup.policy.authorize(
        context or setup.contexts["assistant"], setup.operation,
        {"text": "Hello", **input_fields}, provider_id="example",
    )


def test_exact_grant_is_required_and_issued_without_changing_high_risk(setup_policy):
    setup = setup_policy
    with pytest.raises(IntegrationPolicyError, match="standing grant"):
        _call(setup)
    grant = setup.grants.grant(**setup.bounds)
    assert setup.grants.grant(**setup.bounds).id == grant.id
    invocation = _call(setup)
    assert isinstance(invocation, AuthorizedIntegrationInvocation)
    assert invocation.standing_grant_id == grant.id
    assert invocation.approval_id is None
    assert invocation.operation.risk is RiskLevel.HIGH
    assert invocation.operation.requires_invocation_approval is False
    assert setup.db.scalars(select(InvocationApproval)).all() == []


@pytest.mark.parametrize("field,value", [
    ("operation_id", "example.another.converse"), ("contract_fingerprint", "changed"),
    ("provider", "other"), ("connection_id", 8), ("account_id", "account-b"),
    ("principal_kind", "skill"), ("principal_id", "act"),
])
def test_grant_cannot_cross_any_identity_bound(setup_policy, field, value):
    setup = setup_policy
    grant = setup.grants.grant(**{**setup.bounds, field: value})
    assert setup.grants.active(**setup.bounds) is None
    with pytest.raises(IntegrationStandingGrantError):
        setup.grants.require_bound(grant.id, **setup.bounds)
    with pytest.raises(IntegrationPolicyError, match="standing grant"):
        _call(setup)


def test_revocation_and_reenable_use_new_identity_and_never_restore_old_work(setup_policy):
    setup = setup_policy
    first_id = setup.grants.grant(**setup.bounds).id
    assert setup.grants.revoke(first_id) is True
    assert setup.grants.revoke(first_id) is False
    with pytest.raises(IntegrationPolicyError, match="standing grant"):
        _call(setup)
    second = setup.grants.grant(**setup.bounds)
    assert second.id != first_id
    with pytest.raises(IntegrationStandingGrantError):
        setup.grants.require_bound(first_id, **setup.bounds)
    assert setup.grants.require_bound(second.id, **setup.bounds).id == second.id
    assert setup.grants.revoke_for_principal(
        operation_id=setup.operation.id, principal_kind="agent", principal_id="assistant",
    ) == 1
    assert setup.db.get(IntegrationStandingGrant, first_id).revoked_at is not None


@pytest.mark.parametrize("field,value", [("id", 8), ("account_id", "account-b")])
def test_live_connection_changes_invalidate_grant(setup_policy, field, value):
    setup = setup_policy
    setup.grants.grant(**setup.bounds)
    setattr(setup.connection, field, value)
    with pytest.raises(IntegrationPolicyError, match="standing grant"):
        _call(setup)


@pytest.mark.parametrize("field,value", [("id", None), ("id", True), ("account_id", None), ("account_id", "")])
def test_incomplete_live_connection_identity_cannot_use_a_grant(setup_policy, field, value):
    setup = setup_policy
    setup.grants.grant(**setup.bounds)
    setattr(setup.connection, field, value)
    with pytest.raises(IntegrationPolicyError, match="complete identity"):
        _call(setup)


def test_live_contract_change_invalidates_grant(setup_policy):
    setup = setup_policy
    setup.grants.grant(**setup.bounds)
    changed = replace(setup.operation, contract_version=2)
    setup.policy.registry = _registry(changed)
    with pytest.raises(IntegrationPolicyError, match="standing grant"):
        setup.policy.authorize(setup.contexts["assistant"], changed, {"text": "Hello"}, provider_id="example")


def test_agent_session_and_function_policy_are_rechecked_before_grant(setup_policy):
    setup = setup_policy
    setup.grants.grant(**setup.bounds)
    policy_row = setup.db.get(AgentPolicy, "assistant")
    policy_row.policy_json = {"banned_functions": [setup.operation.id]}
    setup.db.commit()
    with pytest.raises(IntegrationPolicyError, match="Explicitly banned"):
        _call(setup)
    policy_row.policy_json = {"allowed_functions": [setup.operation.id]}
    session = setup.db.get(ActSession, setup.contexts["assistant"].agent_session_id)
    session.status = "archived"
    setup.db.commit()
    with pytest.raises(IntegrationPolicyError, match="session is unavailable"):
        _call(setup)


def test_assistant_grant_does_not_authorize_act_observer_or_direct_user(setup_policy):
    setup = setup_policy
    setup.grants.grant(**setup.bounds)
    for agent in ("act", "observer"):
        with pytest.raises(IntegrationPolicyError):
            _call(setup, setup.contexts[agent])
    with pytest.raises(IntegrationPolicyError, match="unavailable to this caller"):
        _call(setup, InvocationContext(principal_kind="user", origin="http"))
    # A supplied skill identity still has no current caller/version authority.
    with pytest.raises(IntegrationPolicyError):
        _call(setup, InvocationContext(principal_kind="skill", origin="skill_runtime", caller_skill_id=777))


def test_forged_authority_json_and_booleans_cannot_issue_authorized_invocations(setup_policy):
    setup = setup_policy
    grant = setup.grants.grant(**setup.bounds)
    with pytest.raises(IntegrationPolicyError, match="invalid"):
        _call(setup, standing_grant_id=grant.id, approved=True, requires_invocation_approval=False)
    with pytest.raises(TypeError):
        setup.policy.authorize(
            setup.contexts["assistant"], setup.operation, {"text": "Hello"},
            provider_id="example", approved=True,
        )


def test_ordinary_high_operation_keeps_reason_and_per_call_approval(setup_policy, monkeypatch):
    setup = setup_policy
    setup.grants.grant(**setup.bounds)
    ordinary = replace(setup.operation, invocation_approval_mode=InvocationApprovalMode.PER_CALL)
    setup.policy.registry = _registry(ordinary)
    monkeypatch.setattr(
        InvocationApprovalService, "_telegram_service",
        lambda _self: SimpleNamespace(
            approval_available=lambda: True, deliver_invocation_approval=lambda _approval: None,
        ),
    )
    context = InvocationContext(principal_kind="user", origin="http")
    with pytest.raises(IntegrationPolicyError, match="invalid"):
        setup.policy.authorize(context, ordinary, {"text": "Hello"}, provider_id="example")
    receipt = setup.policy.authorize(
        context, ordinary, {"text": "Hello", "reason_to_call": "Requested message"}, provider_id="example",
    )
    assert isinstance(receipt, PendingIntegrationApproval)
    approval = setup.db.get(InvocationApproval, receipt.approval_id)
    assert approval.reason_to_call == "Requested message"
    assert approval.input_json == {"text": "Hello"}


def test_default_operation_contract_identity_remains_byte_equivalent():
    operation = replace(_operation(), invocation_approval_mode=InvocationApprovalMode.PER_CALL)
    identity = operation.contract_identity(["example"])
    assert identity == {
        "provider_selection": "single", "supported_providers": ["example"],
        "input_schema": operation.input_schema, "output_schema": operation.output_schema,
        "effects": ["send"], "resource": {"type": "example.fixed", "identity_fields": []},
        "risk": "high", "version": 1,
    }
    assert _operation().contract_identity(["example"]) == {**identity, "invocation_approval_mode": "standing_grant"}
    registry = _registry(operation)
    assert integration_contract_fingerprint(registry, operation.id, "example") != integration_contract_fingerprint(
        _registry(_operation()), operation.id, "example",
    )
    with pytest.raises(ValueError, match="approval mode"):
        _registry(replace(operation, invocation_approval_mode=False))


def test_catalog_uses_canonical_mode_without_telegram_or_schema_projection(tmp_path, monkeypatch):
    operation = _operation()
    monkeypatch.setattr("app.services.function_catalog_service.DEFAULT_INTEGRATION_REGISTRY", _registry(operation))
    monkeypatch.setattr(
        "app.services.function_catalog_service.build_default_integration_service",
        lambda _db: SimpleNamespace(operation_available=lambda _operation: True),
    )
    monkeypatch.setattr(InvocationApprovalService, "approval_available", lambda _self: False)
    entries = FunctionCatalogService(None, project_root=tmp_path)._fixed_entries()
    entry = next(entry for entry in entries if entry["id"] == operation.id)
    assert entry["risk_level"] == "high"
    assert entry["requires_invocation_approval"] is False
    assert entry["input_schema"] == operation.input_schema
    assert entry["output_schema"] == operation.output_schema
    assert entry["availability"] == "available"


def test_standing_grant_model_has_only_authority_and_lifecycle_fields():
    assert set(IntegrationStandingGrant.__table__.columns.keys()) == {
        "id", "operation_id", "contract_fingerprint", "provider", "connection_id", "account_id",
        "principal_kind", "principal_id", "created_at", "revoked_at",
    }
