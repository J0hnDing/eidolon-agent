from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import ActLiveMessage, ActSession, ActTurn, AgentCredential
from app.schemas.agent_chat import SESSION_TITLE_SCHEMA
from app.schemas.assistant_assessment import ASSESSMENT_RESPONSE_SCHEMA, parse_assessment_response
from app.services.act_app_server_service import (
    agent_app_servers,
    get_agent_app_server,
    is_missing_rollout_error,
    stop_act_app_servers,
)
from app.services.act_browser_runtime_service import ActBrowserRuntimeError, act_browser_runtime
from app.services.agent_policy_service import AgentPolicyService
from app.services.codex_routing_service import CodexRoutingService
from app.services.title_sync_service import TitleSyncService, needs_session_title

logger = logging.getLogger(__name__)

ACT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"response": {"type": "string"}, "title": SESSION_TITLE_SCHEMA},
    "required": ["response", "title"],
    "additionalProperties": False,
}


class ActTurnDispatcher:
    def __init__(self, agent_id: str = "act") -> None:
        self.agent_id = agent_id
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active: dict[int, tuple[str, str]] = {}
        self._session_workers: dict[int, threading.Thread] = {}

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self.recover()
            self._worker = threading.Thread(
                target=self._run,
                name=f"eidolon-{self.agent_id}-turn-dispatcher",
                daemon=True,
            )
            self._worker.start()
            self.notify()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            with self._active_lock:
                active = list(self._active.items())
            for session_id, turn in active:
                try:
                    get_agent_app_server(self.agent_id, session_id).sessions.interrupt_turn(*turn)
                except Exception:
                    pass
            worker = self._worker
            if worker is not None:
                worker.join(timeout=10)
            with self._active_lock:
                session_workers = list(self._session_workers.values())
            deadline = time.monotonic() + 10
            for session_worker in session_workers:
                session_worker.join(timeout=max(0, deadline - time.monotonic()))
            if self.agent_id == "act":
                stop_act_app_servers()
            else:
                agent_app_servers[self.agent_id].stop()
            deadline = time.monotonic() + 5
            for remaining_worker in [worker, *session_workers]:
                if remaining_worker is not None and remaining_worker.is_alive():
                    remaining_worker.join(timeout=max(0, deadline - time.monotonic()))
            self._worker = None

    def notify(self) -> None:
        self._wake.set()

    def recover(self) -> int:
        db = SessionLocal()
        try:
            db.execute(update(AgentCredential).where(AgentCredential.agent_id == self.agent_id).values(revoked=True))
            delivery_ids = list(
                db.scalars(
                    select(ActTurn.id).join(ActSession).where(
                        ActSession.agent_id == self.agent_id,
                        ActTurn.status == "running",
                        ActTurn.delivery_status == "pending",
                    )
                )
            )
            now = datetime.now(UTC)
            result = db.execute(
                update(ActTurn)
                .where(ActTurn.status == "running", ActTurn.session_id.in_(select(ActSession.id).where(ActSession.agent_id == self.agent_id)))
                .values(
                    status="interrupted",
                    error_message="Eidolon restarted while this Act turn was running.",
                    activity_json=[{"kind": "interrupted", "label": "Interrupted by Eidolon restart"}],
                    completed_at=now,
                )
            )
            db.commit()
            recovered = int(result.rowcount or 0)
            db.execute(update(ActLiveMessage).where(
                ActLiveMessage.reply_status == "awaiting",
                ActLiveMessage.turn_id.in_(select(ActTurn.id).join(ActSession).where(
                    ActSession.agent_id == self.agent_id, ActTurn.status == "interrupted",
                )),
            ).values(reply_status="cancelled"))
            db.commit()
        finally:
            db.close()
        for turn_id in delivery_ids:
            self._deliver(turn_id)
        return recovered

    def process_next(self, *, session_id: int | None = None) -> bool:
        db = SessionLocal()
        turn_id: int | None = None
        needs_delivery = False
        try:
            running_sessions = select(ActTurn.session_id).where(ActTurn.status == "running")
            query = (
                select(ActTurn).join(ActSession)
                .where(
                    ActTurn.status == "queued", ActSession.agent_id == self.agent_id,
                    ActSession.status == "active", ActTurn.session_id.not_in(running_sessions),
                )
                .order_by(ActTurn.created_at, ActTurn.id)
                .limit(1)
            )
            if session_id is not None:
                query = query.where(ActTurn.session_id == session_id)
            turn = db.scalar(query)
            if turn is None:
                return False
            claimed = db.execute(
                update(ActTurn)
                .where(
                    ActTurn.id == turn.id, ActTurn.status == "queued",
                    ActTurn.session_id.not_in(running_sessions),
                )
                .values(
                    status="running",
                    started_at=datetime.now(UTC),
                    activity_json=[{"kind": "started", "label": "Act is working"}],
                )
            )
            db.commit()
            if not claimed.rowcount:
                return True
            turn_id = turn.id
            self._execute(db, turn_id)
            db.refresh(turn)
            needs_delivery = turn.delivery_status == "pending"
            return True
        finally:
            db.close()
            if turn_id is not None and needs_delivery:
                self._deliver(turn_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            processed = False
            try:
                if self.agent_id == "act":
                    processed = self._dispatch_act_sessions()
                else:
                    while not self._stop.is_set() and self.process_next():
                        processed = True
            except Exception:
                processed = True
            if not processed:
                self._wake.wait(1.0)
            else:
                self._wake.wait(0.1)
            self._wake.clear()

    def _dispatch_act_sessions(self) -> bool:
        with SessionLocal() as db:
            session_ids = list(db.scalars(
                select(ActTurn.session_id).join(ActSession).where(
                    ActTurn.status == "queued", ActSession.agent_id == "act",
                    ActSession.status == "active",
                ).distinct()
            ))
        started = False
        with self._active_lock:
            for session_id in session_ids:
                if self._stop.is_set():
                    break
                if session_id in self._session_workers:
                    continue
                worker = threading.Thread(
                    target=self._run_act_session, args=(session_id,),
                    name=f"eidolon-act-session-{session_id}", daemon=True,
                )
                self._session_workers[session_id] = worker
                worker.start()
                started = True
        return started

    def _run_act_session(self, session_id: int) -> None:
        try:
            while not self._stop.is_set() and self.process_next(session_id=session_id):
                pass
        except Exception:
            logger.exception("Act session worker failed for session %s", session_id)
        finally:
            with self._active_lock:
                self._session_workers.pop(session_id, None)
            self.notify()

    def _execute(self, db, turn_id: int) -> None:
        turn = db.get(ActTurn, turn_id)
        if turn is None:
            return
        session = db.get(ActSession, turn.session_id)
        if session is None or session.status != "active":
            self._fail(db, turn, "Act session is no longer active")
            return
        app_server = get_agent_app_server(self.agent_id, session.id)
        initial_assessment = False
        try:
            route = "assessment" if self.agent_id == "assistant" else self.agent_id
            routing = CodexRoutingService(db).resolve(role=route, action=route)
            policy = AgentPolicyService(db).policy(self.agent_id)
            model = policy.model or routing.effective_model
            effort = policy.reasoning_effort or routing.effective_reasoning_effort
            recovered_thread = False
            try:
                if session.codex_thread_id.startswith("pending:"):
                    session.codex_thread_id = app_server.start_thread(
                        db, model=model, reasoning_effort=effort, session_id=session.id,
                    )
                    db.commit()
                else:
                    app_server.resume_thread(
                        db, session.codex_thread_id, model=model,
                        reasoning_effort=effort, session_id=session.id,
                    )
            except Exception as exc:
                if not is_missing_rollout_error(exc):
                    raise
                session.codex_thread_id = app_server.start_thread(
                    db,
                    session_id=session.id,
                    model=model,
                    reasoning_effort=effort,
                )
                db.commit()
                recovered_thread = True

            def on_started(codex_turn_id: str) -> None:
                turn.codex_turn_id = codex_turn_id
                db.commit()
                from app.services.agent_proposal_service import AgentProposalService
                AgentProposalService(db).refresh_execution(session.id)
                with self._active_lock:
                    self._active[session.id] = (session.codex_thread_id, codex_turn_id)
                db.refresh(turn)
                if turn.cancel_requested_at is not None or self._stop.is_set():
                    app_server.sessions.interrupt_turn(
                        session.codex_thread_id,
                        codex_turn_id,
                    )

            input_text = _title_input(session, self._turn_input(db, turn, recovered_thread=recovered_thread))
            initial_assessment = (
                self.agent_id == "assistant" and session.origin == "assessment"
                and turn.id == db.scalar(select(ActTurn.id).where(
                    ActTurn.session_id == session.id, ActTurn.backend_message_kind.is_(None),
                ).order_by(ActTurn.id).limit(1))
            )
            response_schema = ASSESSMENT_RESPONSE_SCHEMA if initial_assessment else ACT_RESPONSE_SCHEMA
            try:
                result = app_server.sessions.run_structured_turn(
                    session.codex_thread_id,
                    input_text,
                    response_schema,
                    timeout_seconds=7200 if self.agent_id == "act" else 900,
                    model=model,
                    reasoning_effort=effort,
                    on_turn_started=on_started,
                )
            except Exception as exc:
                db.refresh(turn)
                if recovered_thread or turn.codex_turn_id is not None or not is_missing_rollout_error(exc):
                    raise
                session.codex_thread_id = app_server.start_thread(
                    db,
                    session_id=session.id,
                    model=model,
                    reasoning_effort=effort,
                )
                db.commit()
                recovered_thread = True
                result = app_server.sessions.run_structured_turn(
                    session.codex_thread_id,
                    _title_input(session, self._turn_input(db, turn, recovered_thread=True)),
                    response_schema,
                    timeout_seconds=7200 if self.agent_id == "act" else 900,
                    model=model,
                    reasoning_effort=effort,
                    on_turn_started=on_started,
                )
            db.refresh(turn)
            if turn.cancel_requested_at is not None or self._stop.is_set():
                self._cancelled(db, turn)
            else:
                if initial_assessment:
                    from app.services.assistant_assessment_result_service import AssistantAssessmentResultService

                    parsed = parse_assessment_response(result.output_text)
                    AssistantAssessmentResultService(db).accept(turn, parsed)
                    turn.assistant_message = "Assessment complete."
                else:
                    turn.assistant_message = _response_text(result.output_text)
                turn.activity_json = (
                    ([{"kind": "threadRecovery", "label": "Recovered the Act session"}]
                    if recovered_thread else [])
                    + _activities(result.items)
                )
                session.updated_at = datetime.now(UTC)
                db.commit()
                try:
                    TitleSyncService(db).sync_from_codex(session, app_server.sessions, _response_title(result.output_text))
                except Exception as exc:
                    logger.warning(
                        "Codex title synchronization failed for session %s: %s",
                        session.id,
                        type(exc).__name__,
                    )
                # Chat stops polling at the terminal turn status. Publish the
                # title first so that final poll also receives the new name.
                db.refresh(turn)
                if turn.cancel_requested_at is not None or self._stop.is_set():
                    self._cancelled(db, turn)
                else:
                    turn.status = "succeeded"
                    turn.completed_at = datetime.now(UTC)
                    db.commit()
        except Exception as exc:
            db.refresh(turn)
            if turn.cancel_requested_at is not None or self._stop.is_set():
                self._cancelled(db, turn)
            else:
                self._fail(db, turn, str(exc))
        finally:
            try:
                db.execute(update(ActLiveMessage).where(
                    ActLiveMessage.turn_id == turn.id, ActLiveMessage.reply_status == "awaiting",
                ).values(reply_status="cancelled"))
                AgentPolicyService(db).revoke(session.id)
                db.commit()
                if self.agent_id == "act":
                    try:
                        act_browser_runtime.revoke_session(session.id)
                    except ActBrowserRuntimeError:
                        logger.warning("Act browser connection revocation unavailable for session %s", session.id)
                from app.services.agent_proposal_service import AgentProposalService
                AgentProposalService(db).refresh_execution(session.id)
                if self.agent_id == "assistant":
                    if initial_assessment:
                        from app.services.assistant_assessment_service import AssistantAssessmentService
                        AssistantAssessmentService(db).notify_completed(turn)
                    from app.services.act_session_service import ActSessionError, ActSessionService
                    try:
                        ActSessionService(db, agent_id="assistant").prune_assistant_sessions()
                        db.commit()
                    except ActSessionError:
                        db.rollback()
            finally:
                with self._active_lock:
                    self._active.pop(session.id, None)

    @staticmethod
    def _turn_input(db, turn: ActTurn, *, recovered_thread: bool) -> str:
        inserted = list(db.scalars(select(ActTurn).where(
            ActTurn.session_id == turn.session_id,
            ActTurn.id < turn.id,
            ActTurn.backend_message_kind.is_not(None),
        ).order_by(ActTurn.id)))
        inserted_context = "\n\n".join(
            f"Assistant (backend message): {item.assistant_message or ''}" for item in inserted
        )
        if not recovered_thread:
            return (f"<backend_assistant_messages>\n{inserted_context}\n</backend_assistant_messages>\n\n"
                    if inserted_context else "") + turn.user_message
        completed = list(
            db.scalars(
                select(ActTurn)
                .where(
                    ActTurn.session_id == turn.session_id,
                    ActTurn.id < turn.id,
                    ActTurn.status == "succeeded",
                )
                .order_by(ActTurn.id)
            )
        )
        if not completed:
            return (inserted_context + "\n\n" if inserted_context else "") + turn.user_message
        history = "\n\n".join(
            f"User: {item.user_message}\nAct: {item.assistant_message or ''}"
            for item in [row for row in completed if row.backend_message_kind is None][-20:]
        )
        backend_context = (
            f"<backend_assistant_messages>\n{inserted_context}\n</backend_assistant_messages>\n\n"
            if inserted_context else ""
        )
        return (
            "The previous Codex rollout is unavailable. The following is historical "
            "conversation context only. Do not repeat its actions or tool calls. Continue "
            "from the current request.\n\n"
            f"<historical_conversation>\n{history}\n</historical_conversation>\n\n"
            f"{backend_context}Current user request: {turn.user_message}"
        )

    @staticmethod
    def _cancelled(db, turn: ActTurn) -> None:
        turn.status = "cancelled"
        turn.error_message = None
        turn.activity_json = [{"kind": "cancelled", "label": "Act turn cancelled"}]
        turn.completed_at = datetime.now(UTC)
        db.commit()

    @staticmethod
    def _fail(db, turn: ActTurn, message: str) -> None:
        turn.status = "failed"
        turn.error_message = " ".join(message.split())[:512] or "Act turn failed"
        turn.activity_json = [{"kind": "failed", "label": "Act could not complete the turn"}]
        turn.completed_at = datetime.now(UTC)
        db.commit()

    @staticmethod
    def _deliver(turn_id: int) -> None:
        try:
            from app.services.agent_turn_delivery import deliver_agent_turn_result

            deliver_agent_turn_result(turn_id)
        except Exception:
            return


def _title_input(session: ActSession, input_text: str) -> str:
    metadata = json.dumps({"needsTitle": needs_session_title(session)})
    return (
        f"<eidolon_session_metadata>{metadata}</eidolon_session_metadata>\n"
        "Include the session title in this response when needsTitle is true; otherwise return title: null. "
        "This is response metadata, not a separate task or turn.\n\n" + input_text
    )


def _response_title(value: str) -> object:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed.get("title") if isinstance(parsed, dict) else None


def _response_text(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value.strip()
    if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
        return parsed["response"].strip()
    return value.strip()


def _activities(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    labels: list[dict[str, str]] = []
    display = {
        "commandExecution": "Ran a workspace command",
        "fileChange": "Updated workspace files",
        "mcpToolCall": "Used an Eidolon tool",
        "webSearch": "Researched the web",
    }
    for item in items:
        kind = item.get("type")
        if isinstance(kind, str) and kind in display:
            label = display[kind]
            if kind == "mcpToolCall":
                tool_name = next(
                    (
                        value.strip()
                        for value in (item.get("tool"), item.get("name"))
                        if isinstance(value, str) and value.strip()
                    ),
                    None,
                )
                if tool_name is not None:
                    label = f"Used {tool_name[:120]}"
            labels.append({"kind": kind, "label": label})
    return labels[:20] or [{"kind": "completed", "label": "Act completed the turn"}]


act_turn_dispatcher = ActTurnDispatcher()

agent_dispatchers = {"act": act_turn_dispatcher, "observer": ActTurnDispatcher("observer"), "assistant": ActTurnDispatcher("assistant")}
