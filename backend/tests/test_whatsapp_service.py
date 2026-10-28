import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.execution.context import InvocationContext
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationTargetRef
from app.models import InvocationApproval
from app.routers import whatsapp as routes
from app.services.integration_service import IntegrationService
from app.services.invocation_approval_service import InvocationApprovalService
from app.services.secret_store import FakeSecretStore
from app.services.whatsapp_bridge import WhatsAppError
from app.services.whatsapp_service import WhatsAppService


class Store(FakeSecretStore):
    reads = 0

    def get(self, reference, *, namespace="github"):
        self.reads += 1
        return super().get(reference, namespace=namespace)


class Bridge:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.generation = None
        self.error_type = None
        self.active = False
        self.calls = []
        self.executions = []
        self.fail_send = False
        self.state = {"connected": False, "status": "pairing", "account_id": None,
                      "qr_data_url": "data:image/png;base64,QR_SECRET", "sync_status": "partial"}

    def running(self):
        return self.active

    def start(self, *, key, generation):
        assert len(key) == 44
        self.active = True
        self.generation = generation

    def stop(self):
        self.active = False

    def request(self, method, input_json=None):
        self.calls.append(method)
        if method == "status":
            return self.state.copy()
        if method == "execute":
            self.executions.append(input_json)
            if self.fail_send:
                raise WhatsAppError("outcome_unknown")
            return {"accepted": True, "message_id": "message1", "chat_id": "12025550123@s.whatsapp.net"}
        return {}

    def connect(self, account="account1"):
        self.state.update(connected=True, status="connected", account_id=account, account_login="User",
                          qr_data_url=None)


class Telegram:
    def approval_available(self):
        return True

    def deliver_invocation_approval(self, approval):
        approval.telegram_delivery_status = "delivered"

    def update_invocation_approval(self, approval):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import app.services.whatsapp_service as module

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    bridge, store = Bridge(tmp_path), Store()
    monkeypatch.setattr(module, "whatsapp_bridge", bridge)
    with Session(engine) as db:
        service = WhatsAppService(db, secret_store=store)
        yield db, service, bridge, store
    engine.dispose()


def test_pairing_key_is_write_only_and_qr_only_on_pairing_status(setup):
    db, service, bridge, store = setup
    response = service.pair()
    assert response.qr_data_url.endswith("QR_SECRET")
    assert service.status().qr_data_url is None
    assert len(store.values) == 1
    assert service.connection().account_id == ""
    bridge.connect()
    assert service.status().connected
    assert service.connection().account_id == "account1"
    assert "QR_SECRET" not in service.status().model_dump_json()
    store.reads = 0
    assert service.status().connected
    assert store.reads == 0
    service.clear_cache()
    assert bridge.calls[-2:] == ["clear_cache", "status"]
    assert len(store.values) == 1


def test_restore_crashed_child_and_disconnect_cleans_only_its_directory(setup):
    db, service, bridge, store = setup
    service.pair()
    bridge.connect()
    service.status()
    reference = service.connection().secret_reference
    directory = bridge.root / "runtime" / "whatsapp" / reference
    directory.mkdir(parents=True)
    (directory / "cache.db").write_text("private")
    sibling = directory.parent / "keep"
    sibling.mkdir()
    bridge.active = False
    service.startup()
    assert bridge.running()
    result = service.disconnect()
    assert not result.connected
    assert not directory.exists()
    assert sibling.exists()
    assert not store.values
    assert service.connection() is None
    assert not bridge.running()


def test_failed_credential_cleanup_stays_revoked_and_can_be_retried(setup):
    db, service, bridge, store = setup
    service.pair()
    store.fail_delete = True
    result = service.disconnect()
    assert result.error_type == "cleanup_pending"
    assert service.connection().status == "cleanup_pending"
    with pytest.raises(WhatsAppError, match="could not"):
        service.pair()
    store.fail_delete = False
    service.disconnect()
    assert service.connection() is None


def test_send_defers_execution_then_handles_deny_stale_repeat_and_unknown(setup, monkeypatch, tmp_path):
    db, service, bridge, store = setup
    service.pair()
    bridge.connect()
    service.status()
    integration = IntegrationService(db, project_root=tmp_path, secret_store=store)
    monkeypatch.setattr(InvocationApprovalService, "_telegram_service", lambda self: Telegram())
    import app.execution.handlers.integration as handler

    monkeypatch.setattr(handler, "build_default_integration_service", lambda db: integration)
    executor = InvocationExecutor(db, project_root=tmp_path, integration_service=integration)
    payload = {"recipient": "+12025550123", "text": "Complete message text",
               "reason_to_call": "Send the requested message"}

    def submit():
        return executor.execute(InvocationTargetRef(category="integration", target_id="whatsapp.message.send"),
                                payload, InvocationContext(principal_kind="user", origin="http")).output["approval_id"]

    store.reads = 0
    approval_id = submit()
    assert store.reads == 0
    assert bridge.executions == []
    approval = db.get(InvocationApproval, approval_id)
    assert approval.input_json == {key: value for key, value in payload.items() if key != "reason_to_call"}
    fields = approval.presentation_json["fields"]
    assert any(field["value"] == payload["text"] for field in fields)
    approvals = InvocationApprovalService(db, project_root=tmp_path)
    denied = approvals.deny(submit(), decided_via="local", decided_by="test")
    assert denied.decision_status == "denied"
    assert bridge.executions == []
    approved = approvals.approve(approval_id, decided_via="local", decided_by="test")
    assert approved.execution_status == "succeeded"
    assert bridge.executions[0]["dispatch_id"] == f"approval:{approval_id}"
    assert bridge.executions[0]["account_id"] == "account1"
    approvals.approve(approval_id, decided_via="local", decided_by="test")
    assert len(bridge.executions) == 1
    stale_id = submit()
    bridge.connect("replacement")
    service.status()
    stale = approvals.approve(stale_id, decided_via="local", decided_by="test")
    assert stale.execution_status == "stale"
    unknown_id = submit()
    bridge.fail_send = True
    unknown = approvals.approve(unknown_id, decided_via="local", decided_by="test")
    assert unknown.execution_status == "outcome_unknown"
    approvals.approve(unknown_id, decided_via="local", decided_by="test")
    assert len(bridge.executions) == 2


def test_relink_invalidates_pending_send_even_when_same_account_returns(setup, monkeypatch, tmp_path):
    db, service, bridge, store = setup
    service.pair()
    bridge.connect()
    service.status()
    integration = IntegrationService(db, project_root=tmp_path, secret_store=store)
    monkeypatch.setattr(InvocationApprovalService, "_telegram_service", lambda self: Telegram())
    executor = InvocationExecutor(db, project_root=tmp_path, integration_service=integration)
    outcome = executor.execute(
        InvocationTargetRef(category="integration", target_id="whatsapp.message.send"),
        {"recipient": "+12025550123", "text": "Queued before migration", "reason_to_call": "User request"},
        InvocationContext(principal_kind="user", origin="http"),
    )
    identifier = outcome.output["approval_id"]
    reference = service.connection().secret_reference
    bridge.state.update(connected=False, status="relink_required", qr_data_url=None)
    assert service.status().status == "relink_required"
    assert service.connection().status == "relink_required"
    assert db.get(InvocationApproval, identifier).execution_status == "stale"
    service.pair()
    assert service.connection().secret_reference == reference
    assert service.connection().status == "pairing"
    bridge.connect()
    service.status()
    result = InvocationApprovalService(db, project_root=tmp_path).approve(
        identifier, decided_via="local", decided_by="test",
    )
    assert result.execution_status == "stale"
    assert bridge.executions == []


def test_settings_routes_use_service_and_keep_qr_private(setup, monkeypatch):
    db, service, bridge, store = setup
    service.pair()
    monkeypatch.setattr(routes, "WhatsAppService", lambda db: service)
    monkeypatch.setattr(routes, "FunctionCatalogService", lambda db: SimpleNamespace(refresh=lambda: None))
    assert routes.whatsapp_status(db).qr_data_url is None
    assert routes.whatsapp_pairing(db).qr_data_url is not None
    bridge.connect()
    assert routes.whatsapp_pairing(db).connected


def test_startup_failure_is_optional(setup):
    db, service, bridge, store = setup
    service.pair()
    bridge.active = False
    store.fail_get = True
    service.startup()
    assert bridge.error_type == "secret_store_unavailable"
    assert not service.status().connected


def test_diagnostics_and_explicit_agent_discovery_use_only_private_bridge_methods(setup, monkeypatch):
    db, service, bridge, store = setup
    service.pair()
    bridge.connect()
    monkeypatch.setattr(routes, "WhatsAppService", lambda db: service)
    routes.whatsapp_diagnostics(db)
    assert bridge.calls[-1] == "diagnostics"
    routes.whatsapp_discover_agents(db)
    assert bridge.calls[-1] == "discover_agents"
    assert bridge.executions == []


def test_trusted_history_request_is_bounded_setup_and_requires_connection(setup, monkeypatch):
    from pydantic import ValidationError

    from app.schemas.whatsapp import WhatsAppHistoryRequest

    db, service, bridge, store = setup
    service.pair()
    with pytest.raises(WhatsAppError):
        service.request_history("1807055946647697@bot")
    assert "sync_chat" not in bridge.calls
    bridge.connect()
    monkeypatch.setattr(routes, "WhatsAppService", lambda db: service)
    routes.whatsapp_request_history(WhatsAppHistoryRequest(chat_id="1807055946647697@bot"), db)
    assert bridge.calls[-1] == "sync_chat"
    assert bridge.executions == []
    with pytest.raises(ValidationError):
        WhatsAppHistoryRequest(chat_id="chat", count=10000)
