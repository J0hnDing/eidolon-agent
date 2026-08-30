from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import IntegrationConnection, InvocationApproval
from app.services.gmail_provider import FakeGmailProviderAdapter
from app.services.google_calendar_provider import FakeGoogleCalendarProviderAdapter
from app.services.google_oauth import GoogleOAuthStateStore
from app.services.integration_service import IntegrationService
from app.services.invocation_approval_service import InvocationApprovalService
from app.services.secret_store import FakeSecretStore


class TrackingSecretStore(FakeSecretStore):
    get_count = 0

    def get(self, reference: str, *, namespace: str = "github") -> str:
        self.get_count += 1
        return super().get(reference, namespace=namespace)


class PairedTelegram:
    def approval_available(self) -> bool:
        return True

    def deliver_invocation_approval(self, approval: InvocationApproval) -> None:
        approval.telegram_delivery_status = "delivered"

    def update_invocation_approval(self, approval: InvocationApproval) -> None:
        approval.telegram_delivery_status = "updated"


def test_email_send_is_deferred_stripped_denied_stale_and_recovered(monkeypatch, tmp_path) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = TrackingSecretStore()
        integration = IntegrationService(
            db,
            project_root=tmp_path,
            secret_store=store,
            gmail=FakeGmailProviderAdapter(),
        )
        integration.configure_google_oauth_client("shared-client", "shared-secret")
        store.get_count = 0
        reference = store.put('{"refresh_token":"gmail-refresh"}', namespace="gmail")
        db.add(
            IntegrationConnection(
                provider="gmail",
                secret_store_id=store.implementation_id,
                secret_reference=reference,
                credential_kind="oauth_refresh",
                status="connected",
                account_login="person@example.com",
                account_id="account-1",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                last_validated_at=datetime.now(UTC),
            )
        )
        db.commit()
        gmail = integration.gmail
        assert isinstance(gmail, FakeGmailProviderAdapter)
        monkeypatch.setattr(
            InvocationApprovalService,
            "_telegram_service",
            lambda _self: PairedTelegram(),
        )
        import app.services.integration_service as integration_module

        monkeypatch.setattr(
            integration_module,
            "build_default_integration_service",
            lambda _db: integration,
        )

        payload = {
            "to": ["recipient@example.com"],
            "subject": "Approval test",
            "body": "Complete body",
            "reason_to_call": "Send the requested update",
        }
        receipt = integration.invoke_direct("email.send", payload)
        assert receipt == {"status": "pending_approval", "approval_id": 1}
        assert store.get_count == 0
        assert gmail.calls == []
        approval = db.get(InvocationApproval, 1)
        assert approval is not None
        assert approval.input_json == {key: value for key, value in payload.items() if key != "reason_to_call"}
        assert approval.reason_to_call == payload["reason_to_call"]
        assert approval.presentation_json["preset"] == "email_send"
        assert approval.presentation_json["caller"] == "direct_integration"
        assert approval.presentation_json["reason"] == payload["reason_to_call"]
        assert [field["label"] for field in approval.presentation_json["fields"]] == [
            "To",
            "Subject",
            "Body",
        ]

        denied_receipt = integration.invoke_direct("email.send", payload)
        denied = InvocationApprovalService(db, project_root=tmp_path).deny(
            denied_receipt["approval_id"], decided_via="local", decided_by="tester"
        )
        assert denied.decision_status == "denied"
        assert gmail.calls == []

        approved = InvocationApprovalService(db, project_root=tmp_path).approve(
            approval.id, decided_via="local", decided_by="tester"
        )
        assert approved.execution_status == "succeeded"
        assert gmail.calls == [("email.send", approval.input_json)]
        assert store.get_count == 2

        stale_receipt = integration.invoke_direct("email.send", payload)
        connection = integration._connection("gmail")
        assert connection is not None
        connection.account_id = "account-2"
        db.commit()
        stale = InvocationApprovalService(db, project_root=tmp_path).approve(
            stale_receipt["approval_id"], decided_via="telegram", decided_by="22"
        )
        assert stale.execution_status == "stale"

        interrupted_receipt = integration.invoke_direct("email.send", payload)
        interrupted = db.get(InvocationApproval, interrupted_receipt["approval_id"])
        assert interrupted is not None
        interrupted.decision_status = "approved"
        interrupted.execution_status = "executing"
        db.commit()
        InvocationApprovalService(db, project_root=tmp_path).recover()
        db.refresh(interrupted)
        assert interrupted.execution_status == "outcome_unknown"


def test_gmail_and_calendar_share_oauth_client_but_keep_separate_accounts_and_grants(tmp_path) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        store = FakeSecretStore()
        calendar_states = GoogleOAuthStateStore()
        gmail_states = GoogleOAuthStateStore()
        service = IntegrationService(
            db,
            project_root=tmp_path,
            secret_store=store,
            google_calendar=FakeGoogleCalendarProviderAdapter(
                email="calendar@example.com", account_id="calendar-account"
            ),
            google_oauth_states=calendar_states,
            gmail=FakeGmailProviderAdapter(
                email="mail@example.com", account_id="gmail-account"
            ),
            gmail_oauth_states=gmail_states,
        )
        service.configure_google_oauth_client("shared-client", "shared-secret")
        calendar_url = service.start_google_calendar_oauth()
        gmail_url = service.start_gmail_oauth()
        calendar_state = calendar_url.rsplit("state=", 1)[1]
        gmail_state = gmail_url.rsplit("state=", 1)[1]
        calendar = service.complete_google_calendar_oauth(calendar_state, "calendar-code")
        gmail = service.complete_gmail_oauth(gmail_state, "gmail-code")

        assert calendar.account_email == "calendar@example.com"
        assert gmail.account_email == "mail@example.com"
        assert sorted(store.namespaces.values()) == ["gmail", "google_calendar", "google_oauth"]
        assert service._connection("google_calendar").account_id == "calendar-account"
        assert service._connection("gmail").account_id == "gmail-account"
