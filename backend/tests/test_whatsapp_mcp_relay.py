import asyncio
import io
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.execution.types import InvocationExecutionError, InvocationOutcome
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.models import ActSession, CodexMcpSettings, McpAuditRecord
from app.routers import whatsapp
from app.services.agent_policy_service import AgentPolicyService
from app.services.mcp_function_service import McpFunctionService
from app.services.whatsapp_mcp_client import WhatsAppMcpClient


@pytest.fixture
def relay(monkeypatch):
    entries = [{"id": op.id, "category": "integration", "availability": "available",
                "risk_level": op.risk.value, "mcp_exposed": True}
               for op in DEFAULT_INTEGRATION_REGISTRY.for_provider("whatsapp")]
    monkeypatch.setattr("app.services.function_catalog_service.FunctionCatalogService.list_entries", lambda self: entries)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(CodexMcpSettings(id=1, enabled=True))
        db.add(ActSession(agent_id="act", codex_thread_id="relay-act", status="active"))
        db.commit()
        session = db.scalar(select(ActSession))
        token = AgentPolicyService(db).issue("act", session.id)
        calls = []

        class Executor:
            def execute(self, target, arguments, context):
                # Model the backend's real policy recheck at execution time.
                if context.principal_kind == "agent":
                    AgentPolicyService(db).require_function(context.agent_id, target.target_id)
                calls.append((target, arguments, context))
                return InvocationOutcome(status="succeeded", output={"result": "backend-owned bridge"})

        monkeypatch.setattr(whatsapp, "InvocationExecutor", lambda db: Executor())
        app = FastAPI()
        app.include_router(whatsapp.invocation_router)
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app, client=("127.0.0.1", 12345)) as client:
            yield db, client, token, calls
    engine.dispose()


def test_native_agent_relay_reconstructs_identity_and_rejects_forged_context(relay):
    db, client, token, calls = relay
    payload = {"operation": "whatsapp.chat.list", "input": {"query": "Muse"}}
    response = client.post("/integrations/whatsapp/mcp/invoke", json=payload,
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["output"] == {"result": "backend-owned bridge"}
    assert calls[0][2].agent_id == "act"
    assert calls[0][2].origin == "agent_mcp"
    assert calls[0][1] == {"query": "Muse"}
    forged = client.post("/integrations/whatsapp/mcp/invoke", json={**payload, "context": {"principal_kind": "user"}})
    assert forged.status_code == 422
    assert len(calls) == 1


def test_invalid_agent_does_not_fall_back_to_enabled_public_mcp(relay):
    db, client, token, calls = relay
    payload = {"operation": "whatsapp.chat.list", "input": {}}
    for header in ["Bearer invalid", "Basic invalid"]:
        response = client.post("/integrations/whatsapp/mcp/invoke", json=payload, headers={"Authorization": header})
        assert response.json()["error_type"] == "agent_permission_denied"
    AgentPolicyService(db).revoke(db.scalar(select(ActSession.id)))
    db.commit()
    response = client.post("/integrations/whatsapp/mcp/invoke", json=payload,
                           headers={"Authorization": f"Bearer {token}"})
    assert response.json()["error_type"] == "agent_permission_denied"
    assert calls == []


def test_public_mcp_disabled_origin_and_other_providers_are_rejected(relay):
    db, client, token, calls = relay
    payload = {"operation": "whatsapp.chat.list", "input": {}}
    assert client.post("/integrations/whatsapp/mcp/invoke", json=payload).json()["status"] == "succeeded"
    db.get(CodexMcpSettings, 1).enabled = False
    db.commit()
    assert client.post("/integrations/whatsapp/mcp/invoke", json=payload).json()["error_type"] == "mcp_disabled"
    assert client.post("/integrations/whatsapp/mcp/invoke", json=payload,
                       headers={"Origin": "http://localhost:5174"}).status_code == 403
    assert client.post("/integrations/whatsapp/mcp/invoke", json={"operation": "email.send", "input": {}}).status_code == 404
    assert len(calls) == 1


def test_mcp_whatsapp_uses_backend_transport_with_empty_local_bridge(relay, monkeypatch):
    db, client, token, calls = relay
    from tests.test_mcp_function_service import entry

    entries = [entry("whatsapp.chat.list", "integration", read_only=True)]
    monkeypatch.setattr("app.services.mcp_function_service.FunctionCatalogService.list_entries", lambda self: entries)

    class LocalExecutor:
        def execute(self, *args):
            raise AssertionError("MCP must not use its own process-local WhatsApp runtime")

    class BackendTransport:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8000/integrations/whatsapp/mcp/invoke"
            response = client.post("/integrations/whatsapp/mcp/invoke", content=request.data,
                                   headers=dict(request.header_items()))
            return io.BytesIO(response.content)

    monkeypatch.setattr("app.services.whatsapp_mcp_client.urllib.request.build_opener", lambda *args: BackendTransport())
    service = McpFunctionService(db, agent_token=token, executor=LocalExecutor())
    name = service.tool_name("integration", "whatsapp.chat.list")
    assert service.invoke(name, {"value": "Muse"}).output == {"result": "backend-owned bridge"}
    assert calls[0][2].agent_id == "act"
    audit = db.scalar(select(McpAuditRecord))
    assert audit.status == "succeeded" and audit.agent_id == "act"


@pytest.mark.parametrize("response", [b"not JSON", b"{}", b'{"status":"succeeded","output":"bad"}'])
def test_backend_transport_rejects_malformed_outputs(monkeypatch, response):
    monkeypatch.setattr("app.services.whatsapp_mcp_client.urllib.request.build_opener",
                        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(response)))
    with pytest.raises(InvocationExecutionError) as exc:
        WhatsAppMcpClient().invoke("whatsapp.chat.list", {}, agent_token=None)
    assert exc.value.error_type == "invalid_response"


def test_agent_token_is_not_reflected_on_transport_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("PRIVATE_AGENT_TOKEN")

    monkeypatch.setattr("app.services.whatsapp_mcp_client.urllib.request.build_opener",
                        lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(InvocationExecutionError) as exc:
        WhatsAppMcpClient().invoke("whatsapp.chat.list", {}, agent_token="PRIVATE_AGENT_TOKEN")
    assert exc.value.error_type == "backend_unavailable"
    assert "PRIVATE_AGENT_TOKEN" not in str(exc.value)


def test_background_recovery_publishes_connection_changes_without_settings(monkeypatch):
    import app.main as main
    from app.services.muse_service import MuseService

    monkeypatch.setattr(MuseService, "poll_pending", lambda self: None)

    connected = iter([False, False, True])
    published = []
    tick = 0

    async def sleep(_seconds):
        nonlocal tick
        tick += 1
        if tick > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    monkeypatch.setattr(main, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(main, "WhatsAppService", lambda db: SimpleNamespace(
        startup=lambda: None, status=lambda: SimpleNamespace(connected=next(connected))))
    monkeypatch.setattr(main, "FunctionCatalogService", lambda db: SimpleNamespace(refresh=lambda: published.append(tick)))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.whatsapp_maintenance())
    assert published == [1, 3]
