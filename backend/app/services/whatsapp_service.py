from __future__ import annotations

import base64
import secrets
import shutil
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import IntegrationRuntimeError
from app.models import IntegrationAuthorization, IntegrationConnection, InvocationApproval
from app.schemas.whatsapp import WhatsAppStatus
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store
from app.services.whatsapp_bridge import WhatsAppBridge, WhatsAppError, whatsapp_bridge


class WhatsAppService:
    def __init__(self, db: Session, *, secret_store: SecretStore | None = None,
                 bridge: WhatsAppBridge | None = None) -> None:
        self.db = db
        self.bridge = bridge or whatsapp_bridge
        if secret_store is None:
            try:
                secret_store = default_secret_store()
            except SecretStoreError:
                pass
        self.secret_store = secret_store

    def connection(self) -> IntegrationConnection | None:
        return self.db.scalar(select(IntegrationConnection).where(
            IntegrationConnection.provider == "whatsapp", IntegrationConnection.is_default.is_(True),
        ).order_by(IntegrationConnection.id.desc()).execution_options(populate_existing=True))

    def status(self, *, pairing: bool = False) -> WhatsAppStatus:
        with self.bridge.lock:
            row = self.connection()
            if row is None:
                return WhatsAppStatus()
            if row.status == "cleanup_pending":
                return WhatsAppStatus(status="unavailable", error_type="cleanup_pending")
            if not self.bridge.running() or self.bridge.generation != row.secret_reference:
                return WhatsAppStatus(status="unavailable", account_id=row.account_id or None,
                                      account_login=row.account_login or None,
                                      error_type=self.bridge.error_type or "bridge_unavailable",
                                      **self._muse_permission(row))
            try:
                state = self.bridge.request("status")
            except WhatsAppError as exc:
                return WhatsAppStatus(status="unavailable", error_type=exc.error_type,
                                      account_id=row.account_id or None, **self._muse_permission(row))
            state = {name: state[name] for name in WhatsAppStatus.model_fields if name in state and name != "provider"}
            value = WhatsAppStatus(**state)
            if not pairing:
                value.qr_data_url = None
            if value.connected and value.account_id:
                if row.account_id and row.account_id != value.account_id:
                    self._invalidate("WhatsApp account changed")
                if row.account_id != value.account_id or row.status != "connected":
                    row.account_id = value.account_id
                    row.account_login = value.account_login or value.account_id
                    row.status = "connected"
                    row.last_validated_at = datetime.now(UTC)
                    self.db.commit()
            elif value.status in {"logged_out", "relink_required"} and row.status != value.status:
                row.status = value.status
                self._invalidate("WhatsApp logged out" if value.status == "logged_out"
                                 else "WhatsApp transport requires new pairing")
                self.db.commit()
            for name, state in self._muse_permission(row).items():
                setattr(value, name, state)
            return value

    def _muse_permission(self, row) -> dict:
        from app.services.muse_service import MuseService

        return MuseService(self.db, self).permission(row)

    def configure_muse(self, *, enabled: bool, account_id: str | None) -> WhatsAppStatus:
        from app.services.muse_service import MuseService

        return WhatsAppStatus(**MuseService(self.db, self).configure(enabled=enabled, account_id=account_id))

    def startup(self) -> None:
        with self.bridge.lock:
            row = self.connection()
            if row is not None and row.status not in {"cleanup_pending", "logged_out"}:
                try:
                    self._start(row)
                    if row.status == "pairing":
                        self.bridge.request("pair")
                    self.status()
                except WhatsAppError as exc:
                    self.bridge.error_type = exc.error_type

    def _start(self, row: IntegrationConnection) -> None:
        if self.bridge.running() and self.bridge.generation == row.secret_reference:
            return
        if self.secret_store is None or self.secret_store.implementation_id != row.secret_store_id:
            raise WhatsAppError("secret_store_unavailable")
        try:
            key = self.secret_store.get(row.secret_reference, namespace="whatsapp")
        except SecretStoreError:
            raise WhatsAppError("secret_store_unavailable") from None
        self.bridge.start(key=key, generation=row.secret_reference)

    def pair(self) -> WhatsAppStatus:
        with self.bridge.lock:
            row = self.connection()
            if row is not None and row.status == "cleanup_pending":
                raise WhatsAppError("cleanup_pending")
            if row is not None and row.status == "logged_out":
                self.disconnect()
                row = self.connection()
                if row is not None:
                    raise WhatsAppError("cleanup_pending")
            if row is None:
                if self.secret_store is None:
                    raise WhatsAppError("secret_store_unavailable")
                try:
                    reference = self.secret_store.put(base64.b64encode(secrets.token_bytes(32)).decode(),
                                                      namespace="whatsapp")
                except SecretStoreError:
                    raise WhatsAppError("secret_store_unavailable") from None
                row = IntegrationConnection(provider="whatsapp", secret_store_id=self.secret_store.implementation_id,
                                            secret_reference=reference, credential_kind="encrypted_auth_key",
                                            status="pairing", account_id="", account_login="",
                                            last_validated_at=datetime.now(UTC))
                self.db.add(row)
                try:
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    self.secret_store.delete(reference, namespace="whatsapp")
                    raise
            self._start(row)
            self.bridge.request("pair")
            result = self.status(pairing=True)
            if not result.connected:
                row.status = "pairing"
                self.db.commit()
            return result

    def reconnect(self) -> WhatsAppStatus:
        with self.bridge.lock:
            row = self.connection()
            if row is None or row.status in {"logged_out", "cleanup_pending"}:
                raise WhatsAppError("pairing_required")
            self._start(row)
            self.bridge.request("reconnect")
            return self.status()

    def clear_cache(self) -> WhatsAppStatus:
        with self.bridge.lock:
            row = self.connection()
            if row is None or row.status == "cleanup_pending":
                raise WhatsAppError("connection_unavailable")
            self._start(row)
            self.bridge.request("clear_cache")
            from app.services.muse_service import MuseService

            MuseService(self.db, self).cancel_pending()
            return self.status()

    def diagnostics(self, *, discover_agents: bool = False) -> dict:
        with self.bridge.lock:
            row = self.connection()
            if row is None or row.status in {"cleanup_pending", "logged_out"}:
                raise WhatsAppError("connection_unavailable")
            self._start(row)
            return self.bridge.request("discover_agents" if discover_agents else "diagnostics")

    def disconnect(self) -> WhatsAppStatus:
        with self.bridge.lock:
            row = self.connection()
            if row is None:
                self.bridge.stop()
                return WhatsAppStatus()
            # Revoke authority before attempting remote I/O or credential cleanup.
            row.status = "cleanup_pending"
            self._invalidate("WhatsApp disconnected")
            self.db.commit()
            remote_failed = True
            if self.bridge.running() and self.bridge.generation == row.secret_reference:
                try:
                    remote_failed = bool(self.bridge.request("disconnect").get("remote_logout_failed", False))
                except WhatsAppError:
                    pass
            self.bridge.stop()
            try:
                directory = (self.bridge.root / "runtime" / "whatsapp" / row.secret_reference).resolve()
                parent = (self.bridge.root / "runtime" / "whatsapp").resolve()
                if directory.parent != parent:
                    raise OSError
                if directory.exists():
                    shutil.rmtree(directory)
                if self.secret_store is None or self.secret_store.implementation_id != row.secret_store_id:
                    raise SecretStoreError("unavailable")
                self.secret_store.delete(row.secret_reference, namespace="whatsapp")
            except (OSError, SecretStoreError):
                return WhatsAppStatus(status="unavailable", error_type="cleanup_pending",
                                      remote_logout_failed=remote_failed)
            self.db.delete(row)
            self.db.commit()
            return WhatsAppStatus(remote_logout_failed=remote_failed)

    def request_history(self, chat_id: str) -> dict:
        with self.bridge.lock:
            if not self.status().connected:
                raise WhatsAppError("connection_unavailable")
            return self.bridge.request("sync_chat", {"chat_id": chat_id})

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> dict:
        with self.bridge.lock:
            status = self.status()
            row = self.connection()
            if (not status.connected or row is None or row.id != invocation.connection_id
                    or row.account_id != invocation.provider_account_id):
                raise IntegrationRuntimeError("connection_unavailable", "WhatsApp connection changed or is offline")
            approval_id = invocation.approval_id
            if invocation.operation.id == "whatsapp.muse.converse":
                from app.services.muse_service import MuseService

                return MuseService(self.db, self).execute(invocation)
            if invocation.operation.id == "whatsapp.message.send" and approval_id is None:
                raise IntegrationRuntimeError("approval_required", "WhatsApp sends require per-call approval")
            try:
                return self.bridge.request("execute", {
                    "operation": invocation.operation.id, "input": dict(invocation.input),
                    "account_id": invocation.provider_account_id,
                    "dispatch_id": f"approval:{approval_id}" if approval_id is not None else None,
                })
            except WhatsAppError as exc:
                raise IntegrationRuntimeError(exc.error_type, "WhatsApp operation could not be completed") from None

    def _invalidate(self, reason: str) -> None:
        from app.services.muse_service import MuseService

        MuseService(self.db, self).invalidate(cancel_bridge=False)
        # Pending dispatches must remain stale even if the same account is relinked.
        self.db.execute(update(InvocationApproval).where(
            InvocationApproval.provider == "whatsapp",
            InvocationApproval.execution_status == "not_started",
            InvocationApproval.decision_status.in_(("pending", "approved")),
        ).values(execution_status="stale", error_type="connection_changed",
                 error_message=reason, execution_completed_at=datetime.now(UTC)))
        for authorization in self.db.scalars(select(IntegrationAuthorization).where(
            IntegrationAuthorization.provider == "whatsapp", IntegrationAuthorization.invalidated_at.is_(None),
        )):
            authorization.invalidated_at = datetime.now(UTC)
            authorization.invalidation_reason = reason
            if authorization.approval_request.status in {"pending", "approved"}:
                authorization.approval_request.status = "superseded"
