from __future__ import annotations

from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError

from app.execution.context import InvocationContext
from app.integrations.authorization import _issue_authorized_integration_invocation
from app.integrations.providers.whatsapp import WhatsAppProviderAdapter
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.types import IntegrationEffect, RiskLevel
from app.services.agent_policy_service import AgentPolicyService


def test_whatsapp_operations_are_discovered_with_canonical_effect_and_risk() -> None:
    operations = DEFAULT_INTEGRATION_REGISTRY.for_provider("whatsapp")

    assert {operation.id for operation in operations} == {
        "whatsapp.contact.search",
        "whatsapp.chat.list",
        "whatsapp.message.list",
        "whatsapp.message.search",
        "whatsapp.message.send",
        "whatsapp.muse.converse",
    }
    for operation in operations:
        if operation.id == "whatsapp.muse.converse":
            assert operation.risk is RiskLevel.HIGH
            assert operation.effects == frozenset({IntegrationEffect.READ, IntegrationEffect.SEND})
            assert not operation.read_only and not operation.requires_invocation_approval
            continue
        expected_risk = RiskLevel.HIGH if operation.id.endswith(".send") else RiskLevel.LOW
        assert operation.risk is expected_risk
        assert operation.effects == (
            frozenset({IntegrationEffect.SEND})
            if operation.id.endswith(".send")
            else frozenset({IntegrationEffect.READ})
        )
        assert AgentPolicyService._is_read_only(operation) is (not operation.id.endswith(".send"))

    send = DEFAULT_INTEGRATION_REGISTRY.get("whatsapp.message.send")
    assert send is not None and send.requires_invocation_approval


@pytest.mark.parametrize(
    ("operation_id", "payload"),
    [
        ("whatsapp.contact.search", {"query": "Jordan", "page_size": 50}),
        ("whatsapp.chat.list", {"type": "group", "page_size": 25}),
        ("whatsapp.chat.list", {"type": "group", "query": "Family"}),
        ("whatsapp.chat.list", {"type": "agent"}),
        ("whatsapp.message.list", {"chat_id": "867051314767696@bot"}),
        ("whatsapp.message.search", {"query": "Muse", "chat_id": "867051314767696@bot"}),
        (
            "whatsapp.message.list",
            {"chat_id": "12025550123@s.whatsapp.net", "page_size": 50},
        ),
        (
            "whatsapp.message.search",
            {"query": "meeting", "chat_id": "120255501234567@g.us"},
        ),
        (
            "whatsapp.message.send",
            {"recipient": "+12025550123", "text": "See you at 3."},
        ),
    ],
)
def test_whatsapp_input_contract_accepts_bounded_examples(operation_id: str, payload: dict) -> None:
    operation = DEFAULT_INTEGRATION_REGISTRY.get(operation_id)
    assert operation is not None

    Draft202012Validator(operation.input_schema).validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"recipient": "status@broadcast", "text": "hello"},
        {"recipient": "867051314767696@bot", "text": "hello"},
        {"recipient": "https://example.com", "text": "hello"},
        {"recipient": "12025550123@newsletter", "text": "hello"},
        {"recipient": "+12025550123", "text": "x" * 4001},
        {"recipient": "+12025550123", "text": "hello", "extra": True},
    ],
)
def test_whatsapp_send_contract_rejects_unbounded_or_untrusted_inputs(payload: dict) -> None:
    operation = DEFAULT_INTEGRATION_REGISTRY.get("whatsapp.message.send")
    assert operation is not None

    with pytest.raises(ValidationError):
        Draft202012Validator(operation.input_schema).validate(payload)


def test_whatsapp_send_output_reports_acceptance_without_delivery_claim() -> None:
    operation = DEFAULT_INTEGRATION_REGISTRY.get("whatsapp.message.send")
    assert operation is not None

    Draft202012Validator(operation.output_schema).validate(
        {"accepted": True, "message_id": "BAE5ABCDEF", "chat_id": "12025550123@s.whatsapp.net"}
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(operation.output_schema).validate(
            {"accepted": True, "delivered": True, "message_id": "BAE5ABCDEF", "chat_id": "12025550123@s.whatsapp.net"}
        )


def test_whatsapp_read_output_is_bounded_and_discloses_cache_coverage() -> None:
    operation = DEFAULT_INTEGRATION_REGISTRY.get("whatsapp.message.list")
    assert operation is not None
    schema = Draft202012Validator(operation.output_schema)
    result = {
        "items": [
            {
                "id": "message-1",
                "chat_id": "12025550123@s.whatsapp.net",
                "sender_id": "12025550123@s.whatsapp.net",
                "from_me": False,
                "timestamp": "2026-10-24T12:00:00Z",
                "text": "See you soon.",
                "type": "conversation",
                "reply_to_message_id": None,
            }
        ],
        "next_cursor": None,
        "coverage": {
            "retention_cutoff": "2026-09-24T12:00:00Z",
            "oldest_message_at": "2026-10-01T12:00:00Z",
            "last_synced_at": "2026-10-24T12:01:00Z",
            "sync_status": "connected",
        },
    }

    schema.validate(result)
    result["coverage"].pop("oldest_message_at")
    with pytest.raises(ValidationError):
        schema.validate(result)


def test_whatsapp_adapter_passes_only_authorized_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    operation = DEFAULT_INTEGRATION_REGISTRY.get("whatsapp.contact.search")
    assert operation is not None
    invocation = _issue_authorized_integration_invocation(
        context=InvocationContext(principal_kind="user", origin="http"),
        operation=operation,
        input_json={"query": "Jordan"},
        provider_id="whatsapp",
        provider_account_id="account-1",
        resource=None,
        authorization_id=None,
    )
    db = object()
    secret_store = object()
    observed = {}

    class FakeWhatsAppService:
        def __init__(self, received_db, *, secret_store):  # noqa: ANN001
            observed["db"] = received_db
            observed["secret_store"] = secret_store

        def execute(self, received_invocation):  # noqa: ANN001
            observed["invocation"] = received_invocation
            return {"items": [], "next_cursor": None, "coverage": {}}

    monkeypatch.setattr("app.integrations.providers.whatsapp.WhatsAppService", FakeWhatsAppService)
    adapter = WhatsAppProviderAdapter(
        SimpleNamespace(db=db, secret_store=secret_store)
    )

    result = adapter.execute(invocation)

    assert observed == {"db": db, "secret_store": secret_store, "invocation": invocation}
    assert result.output["items"] == []
    assert result.audit_resource is None
