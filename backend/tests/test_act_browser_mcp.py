from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp import types
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import act_browser_mcp as proxy_module
from app.db import Base
from app.models import ActSession, ActTurn
from app.services.agent_policy_service import AgentPolicyService


def test_preserves_upstream_tool_metadata_and_call_result(monkeypatch):
    requests = []
    metadata = {
        "name": "browser_snapshot", "description": "Page snapshot", "title": "Snapshot",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    }
    result = {"content": [{"type": "text", "text": "page"}], "structuredContent": {"url": "https://example.test"}}

    def request(endpoint, token, payload, **kwargs):
        requests.append((endpoint, token, payload, kwargs))
        return {"tools": [metadata]} if payload["action"] == "mcp_list" else result

    monkeypatch.setattr(proxy_module, "browser_runtime_request", request)
    proxy = proxy_module.ActBrowserMcpProxy("endpoint", "turn-token", "7")
    tool = proxy.list_tools()[0]
    assert tool.inputSchema == metadata["inputSchema"]
    assert tool.annotations.readOnlyHint is True
    assert tool.title == "Snapshot"
    returned = proxy.call_tool("browser_snapshot", {})
    assert returned.content[0].text == "page"
    assert returned.structuredContent == result["structuredContent"]
    assert requests[1] == ("endpoint", "turn-token", {
        "action": "mcp_call", "sessionId": 7, "name": "browser_snapshot", "arguments": {},
    }, {"timeout": 180})


def test_failed_owner_never_exposes_low_level_payload(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("username secret-password raw provider detail")

    monkeypatch.setattr(proxy_module, "browser_runtime_request", fail)
    result = proxy_module.ActBrowserMcpProxy("endpoint", "token", "1").call_tool("browser_click", {})
    assert result.isError is True
    assert result.content[0].text == "Shared Act browser is unavailable or not authorized"


def test_proxy_calls_allow_parallel_independent_sessions(monkeypatch):
    rendezvous = threading.Barrier(2)

    def request(*args, **kwargs):
        rendezvous.wait(timeout=3)
        return {"content": [], "isError": False}

    monkeypatch.setattr(proxy_module, "browser_runtime_request", request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(
            proxy_module.ActBrowserMcpProxy("endpoint", "token", str(session)).call_tool, "browser_snapshot", {},
        ) for session in (1, 2)]
        assert all(result.result(timeout=5).isError is False for result in results)


def test_calls_require_live_act_turn_and_discovery_does_not(monkeypatch):
    checks = []
    proxy = proxy_module.ActBrowserMcpProxy("endpoint", "token", "7", "agent-token")

    def authorize(*, require_turn):
        checks.append(require_turn)
        if require_turn:
            raise RuntimeError("credential revoked secret-token")

    monkeypatch.setattr(proxy, "_authorize", authorize)
    monkeypatch.setattr(proxy_module, "browser_runtime_request", lambda *args, **kwargs: {"tools": []})
    assert proxy.list_tools() == []
    assert proxy.call_tool("browser_navigate", {"url": "https://example.test"}).isError is True
    assert checks == [False, True]


def test_authentication_binds_act_identity_and_session(monkeypatch):
    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def scalar(self, query):
            return 1

    class Policy:
        def __init__(self, db):
            pass

        def authenticate(self, token):
            return "observer", 7

    monkeypatch.setattr(proxy_module, "SessionLocal", Database)
    monkeypatch.setattr(proxy_module, "AgentPolicyService", Policy)
    result = proxy_module.ActBrowserMcpProxy("endpoint", "token", "7", "agent-token").call_tool("browser_snapshot", {})
    assert result.isError is True


@pytest.mark.parametrize("state", ["running", "idle", "canceled", "revoked", "archived"])
@pytest.mark.parametrize("agent_id", ["act", "assistant"])
def test_real_database_checks_current_turn_and_revocation(monkeypatch, tmp_path, state, agent_id):
    engine = create_engine(f"sqlite:///{tmp_path / 'browser-auth.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(proxy_module, "SessionLocal", factory)
    with factory() as db:
        session = ActSession(id=7, agent_id=agent_id, codex_thread_id="browser-test")
        turn = ActTurn(session_id=7, status="running", user_message="test")
        db.add_all([session, turn])
        db.commit()
        token = AgentPolicyService(db).issue(agent_id, 7)
        if state == "idle":
            turn.status = "completed"
        elif state == "canceled":
            from app.models.entities import utc_now
            turn.cancel_requested_at = utc_now()
        elif state == "revoked":
            AgentPolicyService(db).revoke(7)
        elif state == "archived":
            session.status = "archived"
        db.commit()
    requests = []

    def request(*args, **kwargs):
        requests.append(args)
        return {"content": [], "isError": False}

    monkeypatch.setattr(proxy_module, "browser_runtime_request", request)
    result = proxy_module.ActBrowserMcpProxy("endpoint", "token", "7", token).call_tool("browser_snapshot", {})
    assert result.isError is (state != "running")
    assert len(requests) == (1 if state == "running" else 0)
    engine.dispose()


def test_low_level_stdio_server_forwards_result(monkeypatch):
    def request(endpoint, token, payload, **kwargs):
        if payload["action"] == "mcp_list":
            return {"tools": [{"name": "browser_snapshot", "inputSchema": {"type": "object"}}]}
        return {"content": [{"type": "text", "text": "live page"}], "isError": False}

    monkeypatch.setattr(proxy_module, "browser_runtime_request", request)
    server = proxy_module.build_server(proxy_module.ActBrowserMcpProxy("endpoint", "token", "1"))
    result = asyncio.run(server.request_handlers[types.CallToolRequest](types.CallToolRequest(
        params=types.CallToolRequestParams(name="browser_snapshot", arguments={}),
    )))
    assert result.root.content[0].text == "live page"
    assert result.root.isError is False
