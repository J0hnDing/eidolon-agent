from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import InvocationApproval, TelegramBotConnection
from app.services.secret_store import FakeSecretStore
from app.services.telegram_provider import FakeTelegramBotApi, PairingMessage, TelegramProviderError
from app.services.telegram_service import TelegramService, TelegramServiceError


def test_expired_pairing_is_not_reported_as_in_progress() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        service = TelegramService(
            db,
            secret_store=FakeSecretStore(),
            api_factory=lambda _token: FakeTelegramBotApi(),
        )
        service.start_pairing("123:telegram-token")
        connection = db.query(TelegramBotConnection).one()
        connection.pairing_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        status = service.connection_status()

        assert status.connected is False
        assert status.status == "invalid"
        assert status.error_type == "pairing_expired"
        assert status.paired_chat_id is None
        assert status.paired_user_id is None


def test_in_flight_poller_refreshes_replaced_pairing_state() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    store = FakeSecretStore()
    api = FakeTelegramBotApi()
    with Session(engine) as polling_db, Session(engine) as settings_db:
        polling_service = TelegramService(polling_db, secret_store=store, api_factory=lambda _token: api)
        first = polling_service.start_pairing("123:telegram-token")
        stale_row = polling_db.query(TelegramBotConnection).one()
        assert stale_row.pairing_code_hash is not None

        settings_service = TelegramService(settings_db, secret_store=store, api_factory=lambda _token: api)
        replacement = settings_service.start_pairing("123:telegram-token")

        polling_service._handle_pairing(
            stale_row.id,
            stale_row.bot_id,
            PairingMessage(update_id=2, code=replacement.pairing_code, chat_id=11, user_id=22),
        )

        settings_db.expire_all()
        status = settings_service.connection_status()
        assert replacement.pairing_code != first.pairing_code
        assert status.connected is True
        assert status.paired_chat_id == "11"
        assert status.paired_user_id == "22"


def test_polling_conflict_is_transient_and_clean_poll_recovers_false_webhook_status() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = FakeSecretStore()
        api = FakeTelegramBotApi()
        service = TelegramService(db, secret_store=store, api_factory=lambda _token: api)
        pairing = service.start_pairing("123:telegram-token")
        connection = db.query(TelegramBotConnection).one()
        service._handle_pairing(
            connection.id,
            connection.bot_id,
            PairingMessage(update_id=1, code=pairing.pairing_code, chat_id=11, user_id=22),
        )

        api.errors.append(TelegramProviderError("polling_conflict", "another poll is active"))
        with pytest.raises(TelegramServiceError) as exc_info:
            service.poll_once()
        assert exc_info.value.error_type == "polling_conflict"
        db.refresh(connection)
        assert connection.status == "connected"

        connection.status = "webhook_conflict"
        db.commit()
        assert service.poll_once() == 0
        db.refresh(connection)
        assert connection.status == "connected"


def test_persisted_pairing_delivery_callback_and_replay() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = FakeSecretStore()
        api = FakeTelegramBotApi()
        service = TelegramService(db, secret_store=store, api_factory=lambda _token: api)
        pairing = service.start_pairing("123:telegram-token")
        api.updates.append(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 11, "type": "private"},
                    "from": {"id": 22},
                    "text": f"/start {pairing.pairing_code}",
                },
            }
        )
        assert service.poll_once() == 1
        assert service.connection_status().connected is True
        connection = db.query(TelegramBotConnection).one()
        assert connection.last_update_id == 1
        assert connection.pairing_code_hash is None

        approval = InvocationApproval(
            target_kind="integration",
            target_id="email.send",
            target_contract_fingerprint="fingerprint",
            target_description="Send an email",
            provider="gmail",
            provider_account_id="account",
            caller_type="local_user",
            source="direct_integration",
            input_json={"to": ["person@example.com"], "subject": "Subject", "body": "Full body"},
            input_hash="hash",
            reason_to_call="Requested by the user",
            dispatch_metadata_json={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(approval)
        db.commit()
        db.refresh(approval)
        service.deliver_invocation_approval(approval)
        callback_data = api.sent_messages[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
        api.updates.append(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "callback-1",
                    "from": {"id": 22},
                    "data": callback_data,
                    "message": {"message_id": 1, "chat": {"id": 11, "type": "private"}},
                },
            }
        )
        service.poll_once()
        db.refresh(approval)
        assert approval.decision_status == "denied"
        assert approval.decided_via == "telegram"
        assert approval.telegram_delivery_status == "updated"
        assert api.edited_messages[-1]["text"].startswith("❌ <b>Denied</b>")
        assert api.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}
        assert connection.last_update_id == 2

        api.updates.append({**api.updates[-1], "update_id": 3})
        service.poll_once()
        db.refresh(approval)
        assert approval.decision_status == "denied"
        assert connection.last_update_id == 3
