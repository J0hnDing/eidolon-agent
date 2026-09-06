from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationOutcome, InvocationTargetRef
from app.models import ActSession, ActTurn
from app.services.agent_policy_service import AgentPolicyService
from app.services.agent_proposal_service import AgentProposalService


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_invocation_context_is_immutable_and_serializes_no_credentials() -> None:
    context = InvocationContext(
        principal_kind="skill",
        origin="skill_runtime",
        caller_skill_id=7,
        caller_version_id=11,
        caller_run_id=13,
        caller_runtime="function",
        initiating_action="test",
    )

    with pytest.raises(FrozenInstanceError):
        context.caller_skill_id = 99  # type: ignore[misc]

    serialized = context.serialize()
    assert serialized["caller_skill_id"] == 7
    assert not {"token", "credential", "bearer", "secret"}.intersection(serialized)
    assert InvocationContext.deserialize(serialized) == context


def test_agent_credential_factory_resolves_session_and_current_turn(db: Session) -> None:
    session = ActSession(agent_id="assistant", codex_thread_id="assistant-thread")
    db.add(session)
    db.flush()
    turn = ActTurn(session_id=session.id, user_message="Assess", status="running")
    db.add(turn)
    db.commit()
    credential = AgentPolicyService(db).issue("assistant", session.id)

    context = InvocationContextFactory(db).from_agent_credential(credential)

    assert context.principal_kind == "agent"
    assert context.origin == "agent_mcp"
    assert context.agent_id == "assistant"
    assert context.agent_session_id == session.id
    assert context.agent_turn_id == turn.id
    assert credential not in repr(context.serialize())


def test_executor_dispatches_only_by_explicit_category(db: Session) -> None:
    calls = []

    class Handler:
        def execute(self, target, input_json, context):
            calls.append((target, input_json, context))
            return InvocationOutcome(status="succeeded", output={"ok": True})

    executor = InvocationExecutor(db)
    executor.handlers["integration"] = Handler()
    context = InvocationContext(principal_kind="system", origin="backend")

    outcome = executor.execute(
        InvocationTargetRef(category="integration", target_id="same.arbitrary.id"),
        {"value": 1},
        context,
    )

    assert outcome.output == {"ok": True}
    assert calls[0][0].category == "integration"
    assert calls[0][0].target_id == "same.arbitrary.id"


def test_backend_core_download_runs_through_registered_handler(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    expected = {
        "path": "workspace/downloads/example.txt",
        "filename": "example.txt",
        "bytes": 4,
        "media_type": "text/plain",
    }
    monkeypatch.setattr(
        "app.execution.handlers.backend_core.download_document",
        lambda arguments: calls.append(arguments) or expected,
    )

    outcome = InvocationExecutor(db).execute(
        InvocationTargetRef(category="backend_core", target_id="act.document.download"),
        {"source": {"kind": "url", "url": "https://example.com/example.txt"}},
        InvocationContext(principal_kind="user", origin="codex_mcp"),
    )

    assert outcome.output == expected
    assert len(calls) == 1


def test_agent_private_plan_request_uses_authenticated_context(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ActSession(agent_id="assistant", codex_thread_id="assistant-private")
    db.add(session)
    db.commit()
    context = InvocationContext(
        principal_kind="agent",
        origin="agent_mcp",
        agent_id="assistant",
        agent_session_id=session.id,
    )
    seen = []
    monkeypatch.setattr(
        AgentProposalService,
        "submit",
        lambda _self, supplied_context, arguments: (
            seen.append((supplied_context, arguments))
            or SimpleNamespace(status="pending", id=42)
        ),
    )

    outcome = InvocationExecutor(db).execute(
        InvocationTargetRef(category="agent_private", target_id="plan_approval_request"),
        {
            "title": "Plan",
            "rationale": "Useful",
            "actions": "Work",
            "instruction": "Do the work",
        },
        context,
    )

    assert outcome.output == {"status": "pending", "proposal_id": 42}
    assert seen[0][0] is context


def test_web_app_context_factory_does_not_retain_capability(db: Session) -> None:
    token = "WEB_APP_CAPABILITY_SENTINEL"
    runtime = SimpleNamespace(
        instance_for_capability=lambda supplied: (
            SimpleNamespace(id="instance-1", version_id=3),
            SimpleNamespace(id=2),
            SimpleNamespace(),
        )
        if supplied == token
        else pytest.fail("unexpected token"),
    )

    context = InvocationContextFactory(db, web_app_runtime=runtime).from_web_app_capability(token)

    assert context.principal_kind == "web_app"
    assert context.caller_skill_id == 2
    assert context.caller_version_id == 3
    assert context.web_app_instance_id == "instance-1"
    assert token not in repr(context.serialize())


def test_production_adapters_have_no_competing_direct_dispatch() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    banned_identifiers = {
        "current_agent",
        "IntegrationCaller",
        "FunctionCaller",
        "InvocationCallerAttribution",
        "bypass_invocation_approval",
    }
    direct_methods = {
        "invoke_direct",
        "invoke_from_capability",
        "invoke_from_web_app",
        "invoke_approved",
    }
    adapter_paths = [
        *(app_root / "routers").glob("*.py"),
        app_root / "services" / "mcp_function_service.py",
        app_root / "services" / "platform_service.py",
        app_root / "services" / "assistant_assessment_service.py",
        app_root / "services" / "invocation_approval_service.py",
    ]

    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        }
        names.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        )
        assert not banned_identifiers.intersection(names), path

    for path in adapter_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not direct_methods.intersection(called), path
