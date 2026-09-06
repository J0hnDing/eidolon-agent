from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    ActSession,
    ActTelegramBinding,
    ActTurn,
    AgentProposal,
    InvocationApproval,
    TelegramBotConnection,
)
from app.schemas.integration import TelegramConnectionStatus, TelegramPairingResponse
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.invocation_approval_presentation import (
    ApprovalPresentation,
    build_approval_presentation,
    presentation_from_snapshot,
    presentation_snapshot,
)
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store
from app.services.telegram_provider import (
    PairingMessage,
    TelegramApprovalCallback,
    TelegramBotApi,
    TelegramLongPollWorker,
    TelegramNotificationProviderAdapter,
    TelegramProviderError,
    UrllibTelegramBotApi,
    chunk_text_for_telegram,
    create_pairing_code,
    edit_agent_proposal_outcome,
    edit_approval_outcome,
    hash_pairing_code,
    send_agent_proposal_request,
    send_approval_request,
    validate_callback_origin,
)

TELEGRAM_SECRET_NAMESPACE = "telegram"
TELEGRAM_ROLE = "notification_approval"
TELEGRAM_ACT_ROLE = "act_agent"
TELEGRAM_OBSERVER_ROLE = "observer_agent"
TELEGRAM_ASSISTANT_ROLE = "assistant_agent"
TELEGRAM_AGENT_ROLES = (
    TELEGRAM_ACT_ROLE,
    TELEGRAM_OBSERVER_ROLE,
    TELEGRAM_ASSISTANT_ROLE,
)
TELEGRAM_AGENT_IDS = {
    TELEGRAM_ACT_ROLE: "act",
    TELEGRAM_OBSERVER_ROLE: "observer",
    TELEGRAM_ASSISTANT_ROLE: "assistant",
}
TELEGRAM_AGENT_LABELS = {
    TELEGRAM_ACT_ROLE: "Act",
    TELEGRAM_OBSERVER_ROLE: "Observer",
    TELEGRAM_ASSISTANT_ROLE: "Assistant",
}


class TelegramServiceError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class _DatabaseOffsetStore:
    db: Session
    connection_id: int
    bot_id: str

    def _row(self) -> TelegramBotConnection:
        row = self.db.get(TelegramBotConnection, self.connection_id)
        if row is None:
            raise TelegramServiceError("connection_unavailable", "Telegram bot connection changed")
        self.db.refresh(row)
        if row.bot_id != self.bot_id:
            raise TelegramServiceError("connection_unavailable", "Telegram bot connection changed")
        return row

    def get_offset(self) -> int | None:
        row = self._row()
        return None if row.last_update_id is None else row.last_update_id + 1

    def set_offset(self, offset: int) -> None:
        row = self._row()
        row.last_update_id = offset - 1
        self.db.commit()


class TelegramService:
    def __init__(
        self,
        db: Session,
        *,
        secret_store: SecretStore | None = None,
        api_factory: Callable[[str], TelegramBotApi] = UrllibTelegramBotApi,
        role: str = TELEGRAM_ROLE,
    ) -> None:
        self.db = db
        if secret_store is None:
            try:
                secret_store = default_secret_store()
            except SecretStoreError:
                secret_store = None
        self.secret_store = secret_store
        self.api_factory = api_factory
        self.role = role

    def connection_status(self) -> TelegramConnectionStatus:
        row = self._connection()
        if row is None:
            return TelegramConnectionStatus(connected=False, status="disconnected")
        available = self.secret_store is not None and self.secret_store.implementation_id == row.secret_store_id
        status = row.status if available else "unavailable"
        error_type = None if status in {"connected", "pairing"} else status
        pairing_expiry = row.pairing_expires_at
        comparable_expiry = pairing_expiry
        if comparable_expiry is not None and comparable_expiry.tzinfo is None:
            comparable_expiry = comparable_expiry.replace(tzinfo=UTC)
        if status == "pairing" and comparable_expiry is not None and datetime.now(UTC) >= comparable_expiry:
            status = "invalid"
            error_type = "pairing_expired"
        return TelegramConnectionStatus(
            connected=available and status == "connected" and bool(row.paired_chat_id and row.paired_user_id),
            status=status,
            bot_username=row.bot_username,
            paired_chat_id=row.paired_chat_id,
            paired_user_id=row.paired_user_id,
            pairing_expires_at=pairing_expiry,
            last_validated_at=row.updated_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            error_type=error_type,
        )

    def start_pairing(self, token: str) -> TelegramPairingResponse:
        if not token or len(token) > 512:
            raise TelegramServiceError("invalid_credential", "Telegram bot token is invalid")
        if self.secret_store is None:
            raise TelegramServiceError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            api = self.api_factory(token)
            identity = api.get_me()
            webhook = api.get_webhook_info()
            if webhook.get("configured") or webhook.get("url"):
                raise TelegramServiceError("webhook_conflict", "Telegram webhook is configured; long polling is required")
            reference = self.secret_store.put(token, namespace=TELEGRAM_SECRET_NAMESPACE)
        except TelegramServiceError:
            raise
        except TelegramProviderError as exc:
            raise TelegramServiceError(exc.error_type, str(exc)) from None
        except SecretStoreError:
            raise TelegramServiceError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            token = ""
        pairing = create_pairing_code()
        expiry = datetime.fromtimestamp(pairing.expires_at, tz=UTC)
        previous = self._connection()
        previous_reference = previous.secret_reference if previous is not None else None
        now = datetime.now(UTC)
        try:
            if previous is None:
                row = TelegramBotConnection(
                    role=self.role,
                    is_default=True,
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=reference,
                    bot_id=str(identity["id"]),
                    bot_username=identity.get("username"),
                    status="pairing",
                    pairing_code_hash=pairing.code_hash,
                    pairing_expires_at=expiry,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(row)
            else:
                row = previous
                row.secret_store_id = self.secret_store.implementation_id
                row.secret_reference = reference
                row.bot_id = str(identity["id"])
                row.bot_username = identity.get("username")
                row.status = "pairing"
                row.paired_chat_id = None
                row.paired_user_id = None
                row.last_update_id = None
                row.pairing_code_hash = pairing.code_hash
                row.pairing_expires_at = expiry
                row.updated_at = now
            self.db.commit()
            self.db.refresh(row)
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(reference, namespace=TELEGRAM_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise TelegramServiceError("internal_failure", "Telegram pairing could not be saved safely") from None
        if previous_reference and previous_reference != reference:
            try:
                self.secret_store.delete(previous_reference, namespace=TELEGRAM_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        return TelegramPairingResponse(
            connection=self.connection_status(),
            pairing_code=pairing.code,
            expires_at=expiry,
        )

    def remove(self) -> None:
        row = self._connection()
        if row is None:
            return
        if self.secret_store is None or self.secret_store.implementation_id != row.secret_store_id:
            raise TelegramServiceError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(row.secret_reference, namespace=TELEGRAM_SECRET_NAMESPACE)
            binding = self.db.get(ActTelegramBinding, row.id)
            if binding is not None:
                self.db.delete(binding)
            self.db.delete(row)
            self.db.commit()
        except SecretStoreError:
            raise TelegramServiceError("connection_unavailable", "Telegram token could not be removed") from None
        except Exception:
            self.db.rollback()
            raise TelegramServiceError("internal_failure", "Telegram connection could not be removed safely") from None

    def approval_available(self) -> bool:
        return self.connection_status().connected

    def execute_notification(self, input_json: dict[str, Any]) -> dict[str, Any]:
        row, token = self._connected_api_credential()
        try:
            return TelegramNotificationProviderAdapter(self.api_factory).execute(
                "telegram.notification.send",
                input_json,
                token,
                chat_id=row.paired_chat_id,
            )
        finally:
            token = ""

    def deliver_invocation_approval(self, approval: InvocationApproval) -> None:
        row, token = self._connected_api_credential()
        presentation = self._approval_presentation(approval)
        try:
            delivery = send_approval_request(
                self.api_factory(token),
                row.paired_chat_id or "",
                presentation,
            )
        finally:
            token = ""
        approval.telegram_message_ids_json = list(delivery.message_ids)
        approval.telegram_callback_nonce_hash = delivery.nonce_hash
        approval.telegram_delivery_status = "delivered"
        approval.delivered_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(approval)

    def update_invocation_approval(self, approval: InvocationApproval) -> None:
        if not approval.telegram_message_ids_json:
            return
        row, token = self._connected_api_credential()
        presentation = self._approval_presentation(approval)
        try:
            edit_approval_outcome(
                self.api_factory(token),
                row.paired_chat_id or "",
                approval.telegram_message_ids_json[0],
                presentation,
                decision_status=approval.decision_status,
                execution_status=approval.execution_status,
                error_type=approval.error_type,
                error_message=approval.error_message,
            )
        finally:
            token = ""
        approval.telegram_callback_nonce_hash = None
        approval.telegram_delivery_status = "updated"
        self.db.commit()
        self.db.refresh(approval)

    def send_agent_proposal(self, proposal: AgentProposal) -> None:
        if self.role != TELEGRAM_ROLE:
            raise TelegramServiceError(
                "authorization_missing_or_stale",
                "Assistant proposals can only use the Notification / Approval bot",
            )
        row, token = self._connected_api_credential()
        try:
            delivery = send_agent_proposal_request(
                self.api_factory(token),
                row.paired_chat_id or "",
                proposal_id=proposal.id,
                title=proposal.title,
                rationale=proposal.rationale,
                instruction=proposal.instruction,
                actions=proposal.actions,
                references=_proposal_references(proposal.references_json),
            )
        finally:
            token = ""
        proposal.telegram_connection_id = row.id
        proposal.telegram_message_ids_json = list(delivery.message_ids)
        proposal.nonce_hash = delivery.nonce_hash
        proposal.telegram_outcome_fingerprint = None
        self.db.commit()
        self.db.refresh(proposal)

    def update_agent_proposal(self, proposal: AgentProposal) -> None:
        if not proposal.telegram_message_ids_json:
            return
        row, token = self._connected_api_credential()
        if proposal.telegram_connection_id != row.id:
            token = ""
            proposal.nonce_hash = None
            proposal.telegram_outcome_fingerprint = _proposal_outcome_fingerprint(proposal)
            self.db.commit()
            return
        try:
            edit_agent_proposal_outcome(
                self.api_factory(token),
                row.paired_chat_id or "",
                int(proposal.telegram_message_ids_json[0]),
                title=proposal.title,
                rationale=proposal.rationale,
                instruction=proposal.instruction,
                actions=proposal.actions,
                references=_proposal_references(proposal.references_json),
                status=proposal.status,
                execution_status=proposal.execution_status,
            )
        finally:
            token = ""
        proposal.nonce_hash = None
        proposal.telegram_outcome_fingerprint = _proposal_outcome_fingerprint(proposal)
        self.db.commit()
        self.db.refresh(proposal)

    def poll_once(self) -> int:
        row, token = self._configured_api_credential()
        connection_id = row.id
        bot_id = row.bot_id
        try:
            api = self.api_factory(token)
            worker = TelegramLongPollWorker(
                api,
                offset_store=_DatabaseOffsetStore(self.db, connection_id, bot_id),
                expected_chat_id=int(row.paired_chat_id) if row.paired_chat_id is not None else None,
                expected_user_id=int(row.paired_user_id) if row.paired_user_id is not None else None,
                on_pairing=lambda pairing: self._handle_pairing(connection_id, bot_id, pairing),
                on_callback=lambda callback: self._handle_callback(connection_id, bot_id, callback),
                on_message=lambda message: self._handle_message(connection_id, bot_id, message),
                on_error=lambda _error: None,
            )
            worker.ensure_long_polling_ready()
            processed = worker.poll_once()
            self.db.refresh(row)
            if row.status == "webhook_conflict":
                row.status = (
                    "connected"
                    if row.paired_chat_id is not None and row.paired_user_id is not None
                    else "pairing"
                )
                self.db.commit()
            if self.role == TELEGRAM_ROLE and row.status == "connected":
                self._sync_agent_proposals()
            return processed
        except TelegramProviderError as exc:
            if exc.error_type == "webhook_conflict":
                row.status = "webhook_conflict"
                self.db.commit()
            raise TelegramServiceError(exc.error_type, str(exc)) from None
        finally:
            token = ""

    def _handle_pairing(self, connection_id: int, bot_id: str, pairing: PairingMessage) -> None:
        row = self.db.get(TelegramBotConnection, connection_id)
        if row is None:
            return
        self.db.refresh(row)
        if (
            row.bot_id != bot_id
            or row.status != "pairing"
            or row.pairing_code_hash is None
            or row.pairing_expires_at is None
        ):
            return
        expiry = row.pairing_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if datetime.now(UTC) >= expiry or not secrets.compare_digest(
            hash_pairing_code(pairing.code), row.pairing_code_hash
        ):
            return
        row.paired_chat_id = str(pairing.chat_id)
        row.paired_user_id = str(pairing.user_id)
        row.pairing_code_hash = None
        row.pairing_expires_at = None
        row.status = "connected"
        self.db.commit()
        if self.role != TELEGRAM_ROLE:
            return
        unresolved = self.db.scalars(
            select(InvocationApproval)
            .where(InvocationApproval.decision_status == "pending")
            .where(InvocationApproval.execution_status == "not_started")
            .order_by(InvocationApproval.id)
        ).all()
        for approval in unresolved:
            try:
                self.deliver_invocation_approval(approval)
            except Exception:
                self.db.rollback()
        self._sync_agent_proposals(force_pending_delivery=True)

    def _handle_callback(self, connection_id: int, bot_id: str, callback: TelegramApprovalCallback) -> None:
        if self.role != TELEGRAM_ROLE:
            return
        row = self.db.get(TelegramBotConnection, connection_id)
        if row is None:
            return
        self.db.refresh(row)
        if row.bot_id != bot_id or row.paired_chat_id is None or row.paired_user_id is None:
            return
        if callback.kind == "proposal":
            self._handle_proposal_callback(row, callback)
            return
        approval = self.db.get(InvocationApproval, callback.approval_id)
        if approval is None:
            return
        try:
            validate_callback_origin(
                callback,
                expected_chat_id=int(row.paired_chat_id),
                expected_user_id=int(row.paired_user_id),
                expected_nonce_hash=approval.telegram_callback_nonce_hash,
            )
        except TelegramProviderError:
            return
        from app.services.invocation_approval_service import InvocationApprovalService

        service = InvocationApprovalService(self.db)
        if callback.decision == "approve":
            service.approve(approval.id, decided_via="telegram", decided_by=row.paired_user_id)
        else:
            service.deny(approval.id, decided_via="telegram", decided_by=row.paired_user_id)
        approval = self.db.get(InvocationApproval, approval.id)
        if approval is not None:
            if approval.telegram_delivery_status != "updated":
                try:
                    self.update_invocation_approval(approval)
                except Exception:
                    self.db.rollback()
            approval = self.db.get(InvocationApproval, approval.id)
            if approval is not None and approval.telegram_callback_nonce_hash is not None:
                approval.telegram_callback_nonce_hash = None
                self.db.commit()

    def _handle_proposal_callback(
        self,
        row: TelegramBotConnection,
        callback: TelegramApprovalCallback,
    ) -> None:
        proposal = self.db.get(AgentProposal, callback.approval_id)
        if (
            proposal is None
            or proposal.telegram_connection_id != row.id
            or proposal.nonce_hash is None
        ):
            return
        try:
            validate_callback_origin(
                callback,
                expected_chat_id=int(row.paired_chat_id or "0"),
                expected_user_id=int(row.paired_user_id or "0"),
                expected_nonce_hash=proposal.nonce_hash,
            )
        except TelegramProviderError:
            return
        from app.services.agent_proposal_service import AgentProposalService

        AgentProposalService(self.db).decide(
            proposal.id,
            approve=callback.decision == "approve",
        )
        proposal = self.db.get(AgentProposal, proposal.id)
        if proposal is not None and proposal.nonce_hash is not None:
            try:
                self.update_agent_proposal(proposal)
            except Exception:
                self.db.rollback()

    def _sync_agent_proposals(self, *, force_pending_delivery: bool = False) -> None:
        pending = list(
            self.db.scalars(
                select(AgentProposal)
                .where(AgentProposal.status == "pending")
                .order_by(AgentProposal.id)
            )
        )
        for proposal in pending:
            if force_pending_delivery or not proposal.telegram_message_ids_json:
                self.send_agent_proposal(proposal)
        resolved = list(
            self.db.scalars(
                select(AgentProposal)
                .where(AgentProposal.status != "pending")
                .order_by(AgentProposal.id)
            )
        )
        for proposal in resolved:
            if (
                proposal.telegram_message_ids_json
                and proposal.telegram_outcome_fingerprint
                != _proposal_outcome_fingerprint(proposal)
            ):
                self.update_agent_proposal(proposal)

    def _connection(self) -> TelegramBotConnection | None:
        return self.db.scalar(
            select(TelegramBotConnection)
            .where(TelegramBotConnection.role == self.role)
            .where(TelegramBotConnection.is_default.is_(True))
            .order_by(TelegramBotConnection.id.desc())
        )

    def _handle_message(self, connection_id: int, bot_id: str, message: dict[str, Any]) -> None:
        if self.role not in TELEGRAM_AGENT_ROLES:
            return
        row = self.db.get(TelegramBotConnection, connection_id)
        if row is None or row.bot_id != bot_id or row.status != "connected":
            return
        if str(message.get("chat_id")) != row.paired_chat_id or str(message.get("user_id")) != row.paired_user_id:
            return
        text = str(message.get("text") or "").strip()
        if not text or text.startswith("/start"):
            return
        agent_id = TELEGRAM_AGENT_IDS[self.role]
        agent_label = TELEGRAM_AGENT_LABELS[self.role]
        service = ActSessionService(self.db, agent_id=agent_id)
        binding = self.db.get(ActTelegramBinding, connection_id)
        try:
            if text == "/new":
                session = service.create_session(origin="telegram")
                if binding is None:
                    binding = ActTelegramBinding(connection_id=connection_id, active_session_id=session.id)
                    self.db.add(binding)
                else:
                    binding.active_session_id = session.id
                self.db.commit()
                reply = f"Started {agent_label} session #{session.id}."
            elif text == "/sessions":
                sessions = service.list_sessions()
                reply = (
                    "\n".join(f"#{item.id} {item.title}" for item in sessions)
                    or f"No active {agent_label} sessions."
                )
            elif text.startswith("/use"):
                selected = text[4:].strip()
                if not selected.isdigit():
                    reply = "Usage: /use <session id>"
                    self._send_act_reply(row, reply)
                    return
                session = service.read_session(int(selected))
                if session.status != "active":
                    raise ActSessionError(f"That {agent_label} session is archived")
                if binding is None:
                    binding = ActTelegramBinding(connection_id=connection_id, active_session_id=session.id)
                    self.db.add(binding)
                else:
                    binding.active_session_id = session.id
                self.db.commit()
                reply = f"Using {agent_label} session #{session.id}: {session.title}"
            else:
                if binding is not None and binding.active_session_id is None:
                    reply = (
                        f"No {agent_label} session is selected. "
                        "Use /new or /use <session id>."
                    )
                    self._send_agent_reply(row, reply)
                    return
                if binding is None:
                    session = service.create_session(origin="telegram")
                    binding = ActTelegramBinding(
                        connection_id=connection_id,
                        active_session_id=session.id,
                    )
                    self.db.add(binding)
                    self.db.commit()
                turn = service.enqueue_turn(
                    binding.active_session_id,
                    text,
                    delivery_connection_id=row.id,
                    delivery_chat_id=row.paired_chat_id,
                )
                reply = f"Queued {agent_label} turn #{turn.id} in session #{binding.active_session_id}."
            self._send_agent_reply(row, reply)
        except ActSessionError as exc:
            self._send_agent_reply(row, str(exc))

    def _send_act_reply(self, row: TelegramBotConnection, reply: str) -> None:
        self._send_agent_reply(row, reply)

    def _send_agent_reply(self, row: TelegramBotConnection, reply: str) -> None:
        _row, token = self._connected_api_credential()
        try:
            api = self.api_factory(token)
            for chunk in chunk_text_for_telegram(reply):
                api.send_message(int(row.paired_chat_id or "0"), chunk)
        finally:
            token = ""

    def _approval_presentation(self, approval: InvocationApproval) -> ApprovalPresentation:
        snapshot = approval.presentation_json or {}
        presentation = presentation_from_snapshot(approval.id, snapshot)
        if presentation is not None:
            return presentation
        presentation = build_approval_presentation(
            approval_id=approval.id,
            action=approval.target_id,
            caller=approval.source,
            input_json=approval.input_json,
            reason=approval.reason_to_call,
        )
        approval.presentation_json = presentation_snapshot(presentation)
        return presentation

    def _configured_api_credential(self) -> tuple[TelegramBotConnection, str]:
        row = self._connection()
        if row is None or self.secret_store is None or self.secret_store.implementation_id != row.secret_store_id:
            raise TelegramServiceError("connection_unavailable", "Telegram bot is unavailable")
        try:
            return row, self.secret_store.get(row.secret_reference, namespace=TELEGRAM_SECRET_NAMESPACE)
        except SecretStoreError:
            raise TelegramServiceError("connection_unavailable", "Telegram bot token is unavailable") from None

    def _connected_api_credential(self) -> tuple[TelegramBotConnection, str]:
        row, token = self._configured_api_credential()
        if row.status != "connected" or row.paired_chat_id is None or row.paired_user_id is None:
            raise TelegramServiceError("connection_unavailable", "Telegram bot is not paired")
        return row, token


def run_telegram_long_polling(stop_event: threading.Event, *, role: str = TELEGRAM_ROLE) -> None:
    failures = 0
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            TelegramService(db, role=role).poll_once()
            failures = 0
        except TelegramServiceError as exc:
            if exc.error_type == "connection_unavailable":
                delay = 1.0
            else:
                delay = min(60.0, float(2 ** min(failures, 6)))
                failures += 1
            stop_event.wait(delay)
        except Exception:
            failures += 1
            stop_event.wait(min(60.0, float(2 ** min(failures, 6))))
        finally:
            db.close()


TelegramPollers = tuple[threading.Event, tuple[threading.Thread, ...]]


def start_telegram_pollers() -> TelegramPollers:
    stop_event = threading.Event()
    workers = tuple(
        threading.Thread(
            target=run_telegram_long_polling,
            kwargs={"stop_event": stop_event, "role": role},
            name=f"eidolon-telegram-{role}",
            daemon=True,
        )
        for role in (TELEGRAM_ROLE, *TELEGRAM_AGENT_ROLES)
    )
    for worker in workers:
        worker.start()
    return stop_event, workers


def stop_telegram_pollers(pollers: TelegramPollers) -> None:
    stop_event, workers = pollers
    stop_event.set()
    for worker in workers:
        worker.join(timeout=35)


def deliver_act_turn_result(turn_id: int) -> None:
    db = SessionLocal()
    try:
        turn = db.get(ActTurn, turn_id)
        if (
            turn is None
            or turn.delivery_status != "pending"
            or turn.delivery_connection_id is None
            or turn.delivery_chat_id is None
            or turn.status in {"queued", "running"}
        ):
            return
        connection = db.get(TelegramBotConnection, turn.delivery_connection_id)
        session = db.get(ActSession, turn.session_id) if turn is not None else None
        if (
            connection is None
            or session is None
            or TELEGRAM_AGENT_IDS.get(connection.role) != session.agent_id
            or connection.status != "connected"
            or connection.paired_chat_id != turn.delivery_chat_id
        ):
            turn.delivery_status = "failed"
            db.commit()
            return
        service = TelegramService(db, role=connection.role)
        agent_label = TELEGRAM_AGENT_LABELS[connection.role]
        if turn.status == "succeeded":
            reply = turn.assistant_message or f"{agent_label} completed without a text response."
        elif turn.status == "cancelled":
            reply = f"{agent_label} turn #{turn.id} was cancelled."
        else:
            reply = f"{agent_label} turn #{turn.id} failed: {turn.error_message or turn.status}"
        service._send_agent_reply(connection, reply)
        turn.delivery_status = "delivered"
        db.commit()
    except Exception:
        db.rollback()
        turn = db.get(ActTurn, turn_id)
        if turn is not None and turn.delivery_status == "pending":
            turn.delivery_status = "failed"
            db.commit()
    finally:
        db.close()


def _proposal_references(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    references: list[str] = []
    for item in value:
        if isinstance(item, str):
            references.append(item)
        elif isinstance(item, dict):
            reference_type = item.get("type")
            reference_id = item.get("id")
            label = item.get("label")
            if isinstance(label, str) and label:
                references.append(label)
            elif isinstance(reference_type, str) and isinstance(reference_id, (str, int)):
                references.append(f"{reference_type}: {reference_id}")
    return references


def _proposal_outcome_fingerprint(proposal: AgentProposal) -> str:
    return f"{proposal.status}:{proposal.execution_status or ''}"
