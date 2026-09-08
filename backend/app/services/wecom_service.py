"""Trusted backend transport and settings service for the WeCom Observer bot."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    IntegrationConnection,
    WeComInboundMessage,
    WeComObserverBinding,
    WeComObserverUserBinding,
)
from app.schemas.integration import WeComConnectionStatus, WeComPairedUser, WeComPairingResponse
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store
from app.services.wecom_provider import (
    WECOM_AGENT_ID,
    WECOM_HEARTBEAT_SECONDS,
    WECOM_HEARTBEAT_TIMEOUT_SECONDS,
    WECOM_PAIRING_TTL_SECONDS,
    WECOM_SECRET_NAMESPACE,
    WECOM_SUBSCRIBE_TIMEOUT_SECONDS,
    WeComProviderError,
    WeComSocket,
    chunk_text_for_wecom,
    generate_pairing_code,
    hash_pairing_code,
    make_ping_frame,
    make_response_frame,
    make_send_frame,
    make_subscribe_frame,
    open_wecom_websocket,
    parse_frame,
    parse_inbound_message,
    parse_pair_command,
    validate_bot_id,
    validate_secret,
)

WECOM_PROVIDER = "wecom"
WECOM_DELIVERY_WAIT_SECONDS = 15.0


class WeComServiceError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class _DeliveryTask:
    connection_id: int
    user_id: str
    text: str
    done: threading.Event
    error: Exception | None = None


@dataclass(frozen=True)
class _ConnectionConfig:
    connection_id: int
    bot_id: str
    secret_store_id: str
    secret_reference: str


class WeComObserverWorker:
    """Single backend-owned WebSocket worker for the fixed Observer transport."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        secret_store: SecretStore | None = None,
        socket_factory: Callable[[], WeComSocket] = open_wecom_websocket,
    ) -> None:
        self.session_factory = session_factory
        self.secret_store = secret_store
        self.socket_factory = socket_factory
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._queue: queue.Queue[_DeliveryTask] = queue.Queue()
        self._lifecycle_lock = threading.Lock()
        self._socket_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._socket: WeComSocket | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._worker = threading.Thread(
                target=self._run,
                name="eidolon-wecom-observer",
                daemon=True,
            )
            self._worker.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            with self._socket_lock:
                socket = self._socket
                self._socket = None
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            worker = self._worker
            if worker is not None:
                worker.join(timeout=10)
            self._worker = None
            self._fail_queued(WeComProviderError("connection_unavailable", "WeCom worker is stopped"))

    def notify(self) -> None:
        self._wake.set()

    def send_agent_turn_result(self, connection_id: int, user_id: str, text: str) -> None:
        with self._lifecycle_lock:
            running = self._worker is not None and self._worker.is_alive()
        if not running or self._stop.is_set():
            raise WeComProviderError("connection_unavailable", "WeCom Observer transport is not running")
        task = _DeliveryTask(connection_id, user_id, text, threading.Event())
        self._queue.put(task)
        self.notify()
        if not task.done.wait(WECOM_DELIVERY_WAIT_SECONDS):
            raise WeComProviderError("provider_timeout", "WeCom reply delivery timed out")
        if task.error is not None:
            if isinstance(task.error, WeComProviderError):
                raise task.error
            raise WeComProviderError("provider_unavailable", "WeCom reply delivery failed") from task.error

    def _run(self) -> None:
        retry_delay = 1.0
        while not self._stop.is_set():
            config = self._configured_connection()
            if config is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                self._run_connection(config)
                retry_delay = 1.0
            except WeComProviderError as exc:
                self._mark_failure(config, exc)
                retry_delay = 30.0 if exc.error_type == "invalid_credential" else min(60.0, retry_delay * 2)
            except Exception:  # noqa: BLE001 - transport failures must reconnect.
                failure = WeComProviderError("provider_unavailable", "WeCom transport failed")
                self._mark_failure(config, failure)
                retry_delay = min(60.0, retry_delay * 2)
            finally:
                self._close_current_socket()
            if not self._stop.is_set():
                self._wake.wait(retry_delay)
                self._wake.clear()

    def _run_connection(self, config: _ConnectionConfig) -> None:
        socket = self._authenticate(config)
        self._set_current_socket(socket)
        self._mark_authenticated(config)
        delivery: _DeliveryTask | None = None
        delivery_chunks: list[str] = []
        delivery_index = 0
        delivery_request_id: str | None = None
        heartbeat_request_id: str | None = None
        heartbeat_sent_at = 0.0
        next_heartbeat = time.monotonic() + WECOM_HEARTBEAT_SECONDS
        try:
            while not self._stop.is_set():
                if not self._is_current(config):
                    return
                if delivery is None:
                    try:
                        delivery = self._queue.get_nowait()
                    except queue.Empty:
                        delivery = None
                    if delivery is not None:
                        if delivery.connection_id != config.connection_id:
                            self._complete_delivery(
                                delivery,
                                WeComProviderError("connection_unavailable", "WeCom connection changed"),
                            )
                            delivery = None
                        else:
                            delivery_chunks = chunk_text_for_wecom(delivery.text)
                            delivery_index = 0
                            delivery_request_id = None
                if delivery is not None and delivery_request_id is None:
                    frame = make_send_frame(
                        delivery.user_id,
                        delivery_chunks[delivery_index],
                    )
                    delivery_request_id = frame["headers"]["req_id"]
                    socket.send(json.dumps(frame, separators=(",", ":")))
                now = time.monotonic()
                if heartbeat_request_id is not None:
                    if now - heartbeat_sent_at > WECOM_HEARTBEAT_TIMEOUT_SECONDS:
                        raise WeComProviderError("provider_timeout", "WeCom heartbeat was not acknowledged")
                elif now >= next_heartbeat:
                    frame = make_ping_frame()
                    heartbeat_request_id = frame["headers"]["req_id"]
                    heartbeat_sent_at = now
                    next_heartbeat = now + WECOM_HEARTBEAT_SECONDS
                    socket.send(json.dumps(frame, separators=(",", ":")))
                try:
                    raw = socket.recv(timeout=1.0)
                except TimeoutError:
                    continue
                frame = parse_frame(raw)
                inbound = parse_inbound_message(frame)
                if inbound is not None:
                    self._handle_inbound(socket, config, frame)
                    continue
                headers = frame.get("headers")
                response_request_id = headers.get("req_id") if isinstance(headers, dict) else None
                if response_request_id == heartbeat_request_id:
                    if frame.get("errcode", 0) != 0:
                        raise WeComProviderError("provider_unavailable", "WeCom heartbeat was rejected")
                    heartbeat_request_id = None
                    continue
                if delivery is not None and response_request_id == delivery_request_id:
                    if frame.get("errcode", 0) != 0:
                        self._complete_delivery(
                            delivery,
                            WeComProviderError("provider_unavailable", "WeCom rejected the reply"),
                        )
                        delivery = None
                        delivery_request_id = None
                        continue
                    delivery_index += 1
                    if delivery_index >= len(delivery_chunks):
                        self._complete_delivery(delivery, None)
                        delivery = None
                        delivery_request_id = None
                    else:
                        delivery_request_id = None
        finally:
            if delivery is not None and not delivery.done.is_set():
                self._complete_delivery(
                    delivery,
                    WeComProviderError("connection_unavailable", "WeCom connection closed"),
                )

    def _authenticate(self, config: _ConnectionConfig) -> WeComSocket:
        store = self._available_secret_store()
        try:
            secret = store.get(config.secret_reference, namespace=WECOM_SECRET_NAMESPACE)
        except SecretStoreError as exc:
            raise WeComProviderError("connection_unavailable", "WeCom bot secret is unavailable") from exc
        socket: WeComSocket | None = None
        try:
            socket = self.socket_factory()
            request_id = f"aibot_subscribe-{time.time_ns()}"
            socket.send(json.dumps(make_subscribe_frame(config.bot_id, secret, request_id=request_id), separators=(",", ":")))
            deadline = time.monotonic() + WECOM_SUBSCRIBE_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WeComProviderError("provider_timeout", "WeCom authentication timed out")
                frame = parse_frame(socket.recv(timeout=remaining))
                headers = frame.get("headers")
                response_request_id = headers.get("req_id") if isinstance(headers, dict) else None
                if response_request_id != request_id:
                    continue
                if frame.get("errcode", 0) != 0:
                    raise WeComProviderError("invalid_credential", "WeCom bot credentials were rejected")
                return socket
        except WeComProviderError:
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            raise
        except TimeoutError as exc:
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            raise WeComProviderError("provider_timeout", "WeCom authentication timed out") from exc
        except Exception as exc:
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            raise WeComProviderError("provider_unavailable", "WeCom authentication failed") from exc
        finally:
            secret = ""

    def _handle_inbound(
        self,
        socket: WeComSocket,
        config: _ConnectionConfig,
        frame: dict[str, Any],
    ) -> None:
        message = parse_inbound_message(frame)
        if message is None:
            return
        db = self.session_factory()
        try:
            connection = db.get(IntegrationConnection, config.connection_id)
            if (
                connection is None
                or connection.bot_id != config.bot_id
                or connection.secret_reference != config.secret_reference
            ):
                return
            inserted = db.execute(
                insert(WeComInboundMessage)
                .values(connection_id=config.connection_id, message_id=message.message_id)
                .on_conflict_do_nothing(index_elements=["connection_id", "message_id"])
            )
            db.commit()
            if inserted.rowcount == 0:
                return
            binding = db.get(WeComObserverBinding, config.connection_id)
            if binding is None:
                return
            pairing_code = parse_pair_command(message.text)
            if message.chat_type != "single":
                if pairing_code is not None:
                    self._send_callback_reply(
                        socket,
                        message.request_id,
                        "Pairing is only available in a private WeCom chat.",
                    )
                return
            if pairing_code is not None:
                if not self._pairing_code_matches(binding, pairing_code):
                    return
                user_binding = db.scalar(
                    select(WeComObserverUserBinding).where(
                        WeComObserverUserBinding.connection_id == config.connection_id,
                        WeComObserverUserBinding.paired_user_id == message.user_id,
                    )
                )
                if user_binding is None:
                    db.add(
                        WeComObserverUserBinding(
                            connection_id=config.connection_id,
                            paired_user_id=message.user_id,
                        )
                    )
                binding.pairing_code_hash = None
                binding.pairing_expires_at = None
                connection.status = "connected"
                connection.error_type = None
                connection.updated_at = datetime.now(UTC)
                db.commit()
                self._send_callback_reply(socket, message.request_id, "WeCom is paired to this private user.")
                return
            user_binding = db.scalar(
                select(WeComObserverUserBinding).where(
                    WeComObserverUserBinding.connection_id == config.connection_id,
                    WeComObserverUserBinding.paired_user_id == message.user_id,
                )
            )
            if user_binding is None:
                return
            self._handle_observer_message(db, connection, user_binding, message, socket)
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _handle_observer_message(
        self,
        db: Session,
        connection: IntegrationConnection,
        binding: WeComObserverUserBinding,
        message: Any,
        socket: WeComSocket,
    ) -> None:
        service = ActSessionService(db, agent_id=WECOM_AGENT_ID)
        try:
            if message.text == "/new":
                session = service.create_session(origin=WECOM_PROVIDER)
                binding.active_session_id = session.id
                db.commit()
                reply = f"Started Observer session #{session.id}."
            elif message.text == "/sessions":
                sessions = service.list_sessions()
                reply = "\n".join(f"#{item.id} {item.title}" for item in sessions) or "No active Observer sessions."
            elif message.text.startswith("/use"):
                selected = message.text[4:].strip()
                if not selected.isdigit():
                    reply = "Usage: /use <session id>"
                else:
                    session = service.read_session(int(selected))
                    if session.status != "active":
                        raise ActSessionError("That Observer session is archived")
                    binding.active_session_id = session.id
                    db.commit()
                    reply = f"Using Observer session #{session.id}: {session.title}"
            else:
                session = service.resolve_transport_session(
                    binding.active_session_id,
                    origin=WECOM_PROVIDER,
                )
                binding.active_session_id = session.id
                db.commit()
                turn = service.enqueue_turn(
                    session.id,
                    message.text,
                    delivery_provider=WECOM_PROVIDER,
                    delivery_connection_id=connection.id,
                    delivery_chat_id=message.user_id,
                )
                reply = f"Queued Observer turn #{turn.id} in session #{session.id}."
            self._send_callback_reply(socket, message.request_id, reply)
        except ActSessionError as exc:
            db.rollback()
            self._send_callback_reply(socket, message.request_id, str(exc))

    def _send_callback_reply(self, socket: WeComSocket, request_id: str, text: str) -> None:
        chunks = chunk_text_for_wecom(text)
        for index, chunk in enumerate(chunks):
            frame = make_response_frame(
                request_id,
                request_id,
                chunk,
                finish=index == len(chunks) - 1,
            )
            socket.send(json.dumps(frame, separators=(",", ":")))

    def _configured_connection(self) -> _ConnectionConfig | None:
        db = self.session_factory()
        try:
            row = db.scalar(
                select(IntegrationConnection)
                .where(IntegrationConnection.provider == WECOM_PROVIDER)
                .where(IntegrationConnection.is_default.is_(True))
            )
            if row is None or row.status in {"invalid", "disconnected"} or not row.bot_id:
                return None
            return _ConnectionConfig(row.id, row.bot_id, row.secret_store_id, row.secret_reference)
        finally:
            db.close()

    def _available_secret_store(self) -> SecretStore:
        if self.secret_store is None:
            try:
                self.secret_store = default_secret_store()
            except SecretStoreError as exc:
                raise WeComProviderError("connection_unavailable", "Operating-system secret storage is unavailable") from exc
        return self.secret_store

    def _is_current(self, config: _ConnectionConfig) -> bool:
        db = self.session_factory()
        try:
            row = db.get(IntegrationConnection, config.connection_id)
            return bool(
                row is not None
                and row.provider == WECOM_PROVIDER
                and row.bot_id == config.bot_id
                and row.secret_reference == config.secret_reference
                and row.status not in {"invalid", "disconnected"}
            )
        finally:
            db.close()

    def _mark_authenticated(self, config: _ConnectionConfig) -> None:
        db = self.session_factory()
        try:
            row = db.get(IntegrationConnection, config.connection_id)
            if row is None:
                return
            has_users = db.scalar(
                select(WeComObserverUserBinding.id)
                .where(WeComObserverUserBinding.connection_id == config.connection_id)
                .limit(1)
            )
            row.status = "connected" if has_users is not None else "pairing"
            row.error_type = None
            row.last_validated_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            db.commit()
        finally:
            db.close()

    def _mark_failure(self, config: _ConnectionConfig, error: WeComProviderError) -> None:
        if self._stop.is_set():
            return
        db = self.session_factory()
        try:
            row = db.get(IntegrationConnection, config.connection_id)
            if row is None or row.secret_reference != config.secret_reference:
                return
            if error.error_type == "invalid_credential":
                row.status = "invalid"
            elif error.error_type == "connection_unavailable":
                row.status = "unavailable"
            else:
                row.status = "reconnecting"
            row.error_type = error.error_type
            row.updated_at = datetime.now(UTC)
            db.commit()
        finally:
            db.close()

    def _set_current_socket(self, socket: WeComSocket) -> None:
        with self._socket_lock:
            self._socket = socket

    def _close_current_socket(self) -> None:
        with self._socket_lock:
            socket = self._socket
            self._socket = None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _fail_queued(self, error: Exception) -> None:
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                return
            self._complete_delivery(task, error)

    @staticmethod
    def _complete_delivery(task: _DeliveryTask, error: Exception | None) -> None:
        task.error = error
        task.done.set()

    @staticmethod
    def _pairing_code_matches(binding: WeComObserverBinding, code: str) -> bool:
        expiry = binding.pairing_expires_at
        if binding.pairing_code_hash is None or expiry is None:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        from secrets import compare_digest

        return datetime.now(UTC) < expiry and compare_digest(hash_pairing_code(code), binding.pairing_code_hash)


class WeComService:
    """Settings-facing service; transport work remains in the worker above."""

    def __init__(
        self,
        db: Session,
        *,
        secret_store: SecretStore | None = None,
        worker: WeComObserverWorker | None = None,
    ) -> None:
        self.db = db
        self.secret_store = secret_store
        if self.secret_store is None:
            try:
                self.secret_store = default_secret_store()
            except SecretStoreError:
                self.secret_store = None
        self.worker = worker or wecom_observer_worker

    def connection_status(self) -> WeComConnectionStatus:
        row = self.db.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.provider == WECOM_PROVIDER)
            .where(IntegrationConnection.is_default.is_(True))
        )
        if row is None:
            return WeComConnectionStatus(connected=False, status="disconnected")
        binding = self.db.get(WeComObserverBinding, row.id)
        users = list(
            self.db.scalars(
                select(WeComObserverUserBinding)
                .where(WeComObserverUserBinding.connection_id == row.id)
                .order_by(WeComObserverUserBinding.created_at, WeComObserverUserBinding.id)
            )
        )
        status = row.status
        error_type = row.error_type
        if self.secret_store is None or self.secret_store.implementation_id != row.secret_store_id:
            status = "unavailable"
            error_type = "connection_unavailable"
        if status == "connected" and not users:
            status = "pairing"
        if (
            binding is not None
            and not users
            and binding.pairing_expires_at is not None
            and _as_utc(binding.pairing_expires_at) <= datetime.now(UTC)
        ):
            status = "pairing"
            error_type = "pairing_expired"
        return WeComConnectionStatus(
            connected=status == "connected" and bool(users),
            status=status,
            bot_id=row.bot_id or row.account_id,
            paired_users=[
                WeComPairedUser(
                    user_id=user.paired_user_id,
                    active_session_id=user.active_session_id,
                    paired_at=user.created_at,
                )
                for user in users
            ],
            pairing_expires_at=binding.pairing_expires_at if binding is not None else None,
            last_validated_at=row.last_validated_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            error_type=error_type,
        )

    def connect(self, bot_id: str, secret: str) -> WeComPairingResponse:
        try:
            bot_id = validate_bot_id(bot_id)
            secret = validate_secret(secret)
        except WeComProviderError as exc:
            raise WeComServiceError(exc.error_type, str(exc)) from None
        if self.secret_store is None:
            raise WeComServiceError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            new_reference = self.secret_store.put(secret, namespace=WECOM_SECRET_NAMESPACE)
        except SecretStoreError as exc:
            raise WeComServiceError("connection_unavailable", "Operating-system secret storage rejected the secret") from exc
        secret = ""
        old_reference: str | None = None
        try:
            now = datetime.now(UTC)
            expiry = now + timedelta(seconds=WECOM_PAIRING_TTL_SECONDS)
            code = generate_pairing_code()
            row = self.db.scalar(
                select(IntegrationConnection)
                .where(IntegrationConnection.provider == WECOM_PROVIDER)
                .where(IntegrationConnection.is_default.is_(True))
            )
            if row is None:
                row = IntegrationConnection(
                    provider=WECOM_PROVIDER,
                    is_default=True,
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                    credential_kind="wecom_bot_secret",
                    status="connecting",
                    account_login=bot_id,
                    account_id=bot_id,
                    bot_id=bot_id,
                    last_validated_at=now,
                )
                self.db.add(row)
                self.db.flush()
            else:
                old_reference = row.secret_reference
                row.secret_store_id = self.secret_store.implementation_id
                row.secret_reference = new_reference
                row.credential_kind = "wecom_bot_secret"
                row.status = "connecting"
                row.account_login = bot_id
                row.account_id = bot_id
                row.bot_id = bot_id
                row.error_type = None
                row.last_validated_at = now
            binding = self.db.get(WeComObserverBinding, row.id)
            if binding is None:
                binding = WeComObserverBinding(connection_id=row.id)
                self.db.add(binding)
            self.db.query(WeComObserverUserBinding).filter(
                WeComObserverUserBinding.connection_id == row.id
            ).delete(synchronize_session=False)
            binding.pairing_code_hash = hash_pairing_code(code)
            binding.pairing_expires_at = expiry
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace=WECOM_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise WeComServiceError("connection_unavailable", "WeCom connection could not be saved") from exc
        if old_reference and old_reference != new_reference:
            try:
                self.secret_store.delete(old_reference, namespace=WECOM_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        self.worker.notify()
        return WeComPairingResponse(
            connection=self.connection_status(),
            pairing_code=code,
            expires_at=expiry,
        )

    def start_user_pairing(self) -> WeComPairingResponse:
        row = self.db.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.provider == WECOM_PROVIDER)
            .where(IntegrationConnection.is_default.is_(True))
        )
        if row is None:
            raise WeComServiceError("connection_unavailable", "Connect the WeCom Observer bot first")
        binding = self.db.get(WeComObserverBinding, row.id)
        if binding is None:
            binding = WeComObserverBinding(connection_id=row.id)
            self.db.add(binding)
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=WECOM_PAIRING_TTL_SECONDS)
        code = generate_pairing_code()
        binding.pairing_code_hash = hash_pairing_code(code)
        binding.pairing_expires_at = expiry
        row.updated_at = now
        self.db.commit()
        self.worker.notify()
        return WeComPairingResponse(
            connection=self.connection_status(),
            pairing_code=code,
            expires_at=expiry,
        )

    def remove_user(self, user_id: str) -> None:
        row = self.db.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.provider == WECOM_PROVIDER)
            .where(IntegrationConnection.is_default.is_(True))
        )
        if row is None:
            return
        binding = self.db.scalar(
            select(WeComObserverUserBinding).where(
                WeComObserverUserBinding.connection_id == row.id,
                WeComObserverUserBinding.paired_user_id == user_id,
            )
        )
        if binding is not None:
            self.db.delete(binding)
            if not self.db.scalar(
                select(WeComObserverUserBinding.id)
                .where(
                    WeComObserverUserBinding.connection_id == row.id,
                    WeComObserverUserBinding.id != binding.id,
                )
                .limit(1)
            ):
                row.status = "pairing"
            self.db.commit()

    def remove(self) -> None:
        row = self.db.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.provider == WECOM_PROVIDER)
            .where(IntegrationConnection.is_default.is_(True))
        )
        if row is None:
            return
        if self.secret_store is None or self.secret_store.implementation_id != row.secret_store_id:
            raise WeComServiceError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(row.secret_reference, namespace=WECOM_SECRET_NAMESPACE)
        except SecretStoreError as exc:
            raise WeComServiceError("connection_unavailable", "WeCom bot secret could not be removed") from exc
        self.db.query(WeComInboundMessage).filter(WeComInboundMessage.connection_id == row.id).delete(
            synchronize_session=False
        )
        binding = self.db.get(WeComObserverBinding, row.id)
        if binding is not None:
            self.db.delete(binding)
        self.db.delete(row)
        self.db.commit()
        self.worker.notify()


def start_wecom_observer_worker() -> None:
    wecom_observer_worker.start()


def stop_wecom_observer_worker() -> None:
    wecom_observer_worker.stop()


wecom_observer_worker = WeComObserverWorker()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
