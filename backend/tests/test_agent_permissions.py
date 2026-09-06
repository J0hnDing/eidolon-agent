import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.execution.types import InvocationExecutionError, InvocationOutcome
from app.models import ActSession, InvocationApproval, McpAuditRecord
from app.schemas.agents import AgentPolicyUpdate
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
from app.services.function_catalog_service import FunctionCatalogService
from app.services.invocation_approval_service import InvocationApprovalService
from app.services.mcp_function_service import McpFunctionError, McpFunctionService


@pytest.fixture
def context(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    entries = [
        dict(
            id=name,
            category="integration",
            title=name,
            description=name,
            risk_level=risk,
            availability="available",
            mcp_exposed=True,
            mcp_read_only=read_only,
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
        )
        for name, risk, read_only in [("read", "high", True), ("write", "low", False)]
    ]
    monkeypatch.setattr(FunctionCatalogService, "list_entries", lambda _self: entries)
    with Session(engine) as db:
        yield db, entries


def credential(db, agent_id):
    session = ActSession(agent_id=agent_id, codex_thread_id=f"thread-{agent_id}")
    db.add(session)
    db.commit()
    return AgentPolicyService(db).issue(agent_id, session.id)


def test_policy_filters_semantics_and_risk_and_bans_override_allows(context):
    db, entries = context
    policy = AgentPolicyService(db)
    assert policy.decision("observer", entries[0])[0]
    assert not policy.decision("observer", entries[1])[0]
    policy.update("observer", AgentPolicyUpdate(max_risk="low", allowed_functions=["write"]))
    assert not policy.decision("observer", entries[0])[0]
    assert policy.decision("observer", entries[1])[0]
    policy.update("observer", AgentPolicyUpdate(allowed_functions=["write"], banned_functions=["write"]))
    assert not policy.decision("observer", entries[1])[0]
    with pytest.raises(AgentPermissionError):
        policy.update("observer", AgentPolicyUpdate(allowed_functions=["plan_approval_request"]))


def test_assistant_allows_read_only_new_email(context):
    db, entries = context
    email_entry = {
        "id": "email.read_new",
        "category": "integration",
        "title": "Read new email",
        "description": "Read new email without changing its read state.",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object"},
        "availability": "available",
        "mcp_exposed": True,
        "mcp_read_only": True,
        "mcp_destructive": False,
        "mcp_open_world": False,
        "risk_level": "low",
    }
    entries.append(email_entry)

    described = AgentPolicyService(db).describe("assistant")
    listed = next(item for item in described["functions"] if item["id"] == "email.read_new")
    assert listed["allowed"] is True
    assert listed["reason"] == "Allowed by default policy"
    assistant_tools = McpFunctionService(db, agent_token=credential(db, "assistant")).list_tools()
    assert McpFunctionService.tool_name("integration", "email.read_new") in {tool.name for tool in assistant_tools}


def test_agent_mcp_identity_discovery_live_revocation_and_audit(context, monkeypatch):
    db, entries = context
    token = credential(db, "observer")
    service = McpFunctionService(db, agent_token=token)
    name = McpFunctionService.tool_name("integration", "read")
    assert [tool.name for tool in service.list_tools()] == [name]
    calls = []
    class Executor:
        def execute(self, target, arguments, invocation_context):
            try:
                AgentPolicyService(db).require_function(
                    invocation_context.agent_id,
                    target.target_id,
                )
            except AgentPermissionError as exc:
                raise InvocationExecutionError(exc.error_type, str(exc)) from None
            calls.append((target, arguments, invocation_context))
            return InvocationOutcome(status="succeeded", output={})

    service.executor = Executor()  # type: ignore[assignment]
    service.invoke(name, {})
    audit = db.scalar(select(McpAuditRecord))
    assert audit.agent_id == "observer"
    assert audit.agent_session_id is not None
    AgentPolicyService(db).update("observer", AgentPolicyUpdate(banned_functions=["read"]))
    assert service.list_tools() == []
    with pytest.raises(McpFunctionError, match="Explicitly banned"):
        service.invoke(name, {})
    assert len(calls) == 1
    AgentPolicyService(db).revoke(audit.agent_session_id)
    db.commit()
    with pytest.raises(McpFunctionError, match="revoked"):
        service.invoke(name, {})
    with pytest.raises(AgentPermissionError):
        McpFunctionService(db, agent_token="observer")


def test_plan_request_is_never_public_or_observer_tool(context):
    db, _entries = context
    private = McpFunctionService.tool_name("agent_private", "plan_approval_request")
    assert private not in {tool.name for tool in McpFunctionService(db).list_tools()}
    assert private not in {
        tool.name for tool in McpFunctionService(db, agent_token=credential(db, "observer")).list_tools()
    }
    assert private in {
        tool.name for tool in McpFunctionService(db, agent_token=credential(db, "assistant")).list_tools()
    }


def test_deferred_approval_rechecks_agent_policy(context, monkeypatch, tmp_path):
    db, _entries = context
    session = ActSession(agent_id="observer", codex_thread_id="thread-observer")
    db.add(session)
    db.commit()
    approval = InvocationApproval(
        target_kind="integration",
        target_id="write",
        target_contract_fingerprint="test",
        target_description="write",
        caller_type="codex_mcp",
        source="codex_mcp",
        input_json={},
        input_hash="test",
        reason_to_call="test",
        decision_status="approved",
        execution_status="executing",
        dispatch_metadata_json={"agent_identity": {"agent_id": "observer", "session_id": session.id}},
    )
    db.add(approval)
    db.commit()
    result = InvocationApprovalService(db, project_root=tmp_path)._dispatch_claimed(approval)
    assert result.execution_status == "failed"
    assert result.error_type == "agent_permission_denied"
    AgentPolicyService(db).update("observer", AgentPolicyUpdate(allowed_functions=["write"]))
    session.status = "archived"
    approval.execution_status = "executing"
    approval.error_type = None
    approval.error_message = None
    db.commit()
    result = InvocationApprovalService(db, project_root=tmp_path)._dispatch_claimed(approval)
    assert result.error_type == "agent_permission_denied"
    assert "session" in result.error_message
