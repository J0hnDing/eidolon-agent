"""Atlas CRUD retains canonical discovery, agent policy and invocation approvals."""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.integrations.atlas_attachments import ATLAS_ATTACHMENT_SPECS
from app.integrations.atlas_lifecycle import ATLAS_LIFECYCLE_SPECS
from app.integrations.atlas_state_write import ATLAS_STATE_WRITE_SPECS
from app.integrations.types import RiskLevel
from app.models import ActSession, ActTurn, InvocationApproval, McpAuditRecord
from app.schemas.manifest import SkillManifest
from app.services.agent_policy_service import AgentPolicyService
from app.services.atlas_provider import FakeAtlasProviderAdapter
from app.services.function_catalog_service import FunctionCatalogService
from app.services.integration_service import IntegrationError, IntegrationService
from app.services.invocation_approval_service import InvocationApprovalService
from app.services.mcp_function_service import McpFunctionService
from app.services.secret_store import FakeSecretStore
from tests.test_integration_service import authorize, caller_context, create_installed_skill, invoke

NEW_SPECS = (*ATLAS_LIFECYCLE_SPECS, *ATLAS_STATE_WRITE_SPECS, *ATLAS_ATTACHMENT_SPECS)
ORDINARY_SPECS = tuple(spec for spec in NEW_SPECS if spec.risk != RiskLevel.HIGH)
HIGH_SPECS = tuple(spec for spec in NEW_SPECS if spec.risk == RiskLevel.HIGH)


class PairedTelegram:
    def approval_available(self):
        return True

    def deliver_invocation_approval(self, approval):
        approval.telegram_delivery_status = "delivered"

    def update_invocation_approval(self, approval):
        approval.telegram_delivery_status = "updated"


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        atlas = FakeAtlasProviderAdapter()
        integration = IntegrationService(db, project_root=tmp_path, secret_store=FakeSecretStore(), atlas=atlas)
        monkeypatch.setattr(integration, "provider_connected", lambda provider: provider == "atlas")
        monkeypatch.setattr(InvocationApprovalService, "_telegram_service", lambda _self: PairedTelegram())
        monkeypatch.setattr("app.services.function_catalog_service.build_default_integration_service", lambda _db: integration)
        monkeypatch.setattr("app.execution.handlers.integration.build_default_integration_service", lambda _db: integration)
        # Exercise the actual registry -> catalog projection, using an isolated catalog file.
        entries = FunctionCatalogService(db, project_root=tmp_path).list_entries(refresh=True)
        monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda _self, **_kwargs: entries)
        executor = InvocationExecutor(db, project_root=tmp_path, integration_service=integration)
        yield db, atlas, integration, executor, tmp_path
    engine.dispose()


def agent(db, agent_id):
    session = ActSession(agent_id=agent_id, codex_thread_id=f"crud-{agent_id}")
    db.add(session)
    db.flush()
    db.add(ActTurn(session_id=session.id, user_message="Manage my Atlas data", status="running"))
    db.commit()
    token = AgentPolicyService(db).issue(agent_id, session.id)
    return token, InvocationContextFactory(db).from_agent_credential(token)


@pytest.mark.parametrize("spec", ORDINARY_SPECS, ids=lambda spec: spec.id)
def test_assistant_discovers_and_invokes_new_operations_through_mcp_and_kernel(context, spec):
    db, atlas, _integration, executor, root = context
    token, _agent_context = agent(db, "assistant")
    mcp = McpFunctionService(db, project_root=root, agent_token=token, executor=executor)
    tools = {tool.name: tool for tool in mcp.list_tools()}
    name = mcp.tool_name("integration", spec.id)
    assert name in tools
    assert tools[name].annotations.readOnlyHint == spec.read_only
    assert tools[name].annotations.destructiveHint == spec.destructive
    payload = spec.presentation.usage_example["input"]
    result = mcp.invoke(name, payload)
    assert isinstance(result.output, dict)
    assert atlas.calls == [(spec.id, payload)]
    audit = db.scalar(select(McpAuditRecord).where(McpAuditRecord.function_id == spec.id))
    assert audit.status == "succeeded"
    assert audit.agent_id == "assistant" and audit.agent_session_id is not None and audit.agent_turn_id is not None


@pytest.mark.parametrize("spec", tuple(spec for spec in NEW_SPECS if not spec.read_only), ids=lambda spec: spec.id)
def test_observer_cannot_discover_or_invoke_new_atlas_writes(context, spec):
    db, atlas, _integration, executor, root = context
    token, observer_context = agent(db, "observer")
    mcp = McpFunctionService(db, project_root=root, agent_token=token, executor=executor)
    name = mcp.tool_name("integration", spec.id)
    assert name not in {tool.name for tool in mcp.list_tools()}
    payload = dict(spec.presentation.usage_example["input"])
    if spec.requires_invocation_approval:
        payload["reason_to_call"] = "User requested this deletion"
    # Direct kernel invocation cannot bypass the discovery restriction.
    with pytest.raises(InvocationExecutionError) as denied:
        executor.execute(InvocationTargetRef(category="integration", target_id=spec.id), payload, observer_context)
    assert denied.value.error_type == "agent_permission_denied"
    assert atlas.calls == []
    assert db.scalar(select(InvocationApproval)) is None


@pytest.mark.parametrize("spec", HIGH_SPECS, ids=lambda spec: spec.id)
def test_irreversible_operations_wait_for_per_call_approval_then_dispatch_once(context, spec):
    db, atlas, _integration, executor, root = context
    token, _agent_context = agent(db, "assistant")
    mcp = McpFunctionService(db, project_root=root, agent_token=token, executor=executor)
    name = mcp.tool_name("integration", spec.id)
    assert name in {tool.name for tool in mcp.list_tools()}
    business_input = spec.presentation.usage_example["input"]
    payload = {**business_input, "reason_to_call": "Permanently remove the data the user selected"}
    receipt = mcp.invoke(name, payload).output
    assert receipt["status"] == "pending_approval"
    assert atlas.calls == []
    approval = db.get(InvocationApproval, receipt["approval_id"])
    assert approval.input_json == business_input
    assert approval.reason_to_call == payload["reason_to_call"]
    assert approval.target_id == spec.id
    security = approval.dispatch_metadata_json["integration_security_v2"]
    assert security["provider"] == "atlas" and security["risk"] == "high" and security["effects"] == ["delete"]
    assert approval.dispatch_metadata_json["invocation_context_v1"]["agent_id"] == "assistant"
    approvals = InvocationApprovalService(db, project_root=root)
    approved = approvals.approve(approval.id, decided_via="local", decided_by="tester")
    assert approved.execution_status == "succeeded"
    assert atlas.calls == [(spec.id, business_input)]
    approvals.approve(approval.id, decided_via="local", decided_by="tester")
    assert atlas.calls == [(spec.id, business_input)]
    # Standing agent access and a previous approved call do not authorize another deletion.
    next_receipt = mcp.invoke(name, payload).output
    assert next_receipt["status"] == "pending_approval" and next_receipt["approval_id"] != approval.id
    assert atlas.calls == [(spec.id, business_input)]
    denied = approvals.deny(next_receipt["approval_id"], decided_via="local", decided_by="tester")
    assert denied.decision_status == "denied"
    assert atlas.calls == [(spec.id, business_input)]


def test_new_operation_is_undeclared_then_requires_fresh_selected_authorization(context):
    db, atlas, integration, _executor, root = context
    old_requirement = {"provider": "atlas", "operations": ["atlas.records.update"], "resource_scope": {}}
    skill, manifest = create_installed_skill(db, root, requirement=old_requirement)
    old_authorization = authorize(integration, skill, manifest)
    old_input = {"id": "record-1", "expected_revision": 1, "title": "Updated"}
    invoke(integration, "atlas.records.update", old_input, caller_context(skill))
    atlas.calls.clear()
    payload = {"id": "record-1", "expected_revision": 2}
    with pytest.raises(IntegrationError) as undeclared:
        invoke(integration, "atlas.records.trash", payload, caller_context(skill))
    assert undeclared.value.error_type == "operation_undeclared"
    assert atlas.calls == []
    expanded = SkillManifest.model_validate({
        **manifest.model_dump(mode="json"),
        "integration_requirements": [{**old_requirement, "operations": ["atlas.records.update", "atlas.records.trash"]}],
    })
    manifest_path = root / skill.manifest_path
    manifest_path.write_text(json.dumps(expanded.model_dump(mode="json")), encoding="utf-8")
    new_authorization = integration.ensure_authorization_requests(skill, expanded)[0]
    assert new_authorization.id != old_authorization.id
    assert new_authorization.approval_request.status == "pending"
    with pytest.raises(IntegrationError) as stale:
        invoke(integration, "atlas.records.trash", payload, caller_context(skill))
    assert stale.value.error_type == "authorization_missing_or_stale"
    assert atlas.calls == []
    new_authorization.approval_request.status = "approved"
    db.commit()
    assert invoke(integration, "atlas.records.trash", payload, caller_context(skill))["record"]["id"] == "record-1"
    assert atlas.calls == [("atlas.records.trash", payload)]
