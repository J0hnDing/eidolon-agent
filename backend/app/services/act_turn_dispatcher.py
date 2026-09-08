from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import ActSession, ActTurn, AgentCredential
from app.services.act_app_server_service import act_app_server_service, agent_app_servers, is_missing_rollout_error
from app.services.agent_policy_service import AgentPolicyService
from app.services.codex_routing_service import CodexRoutingService

ACT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"response": {"type": "string"}},
    "required": ["response"],
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
        self._active: tuple[str, str] | None = None

    @property
    def app_server(self):
        return act_app_server_service if self.agent_id == "act" else agent_app_servers[self.agent_id]

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            self.recover()
            self._worker = threading.Thread(
                target=self._run,
                name="eidolon-act-turn-dispatcher",
                daemon=True,
            )
            self._worker.start()
            self.notify()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            with self._active_lock:
                active = self._active
            if active is not None:
                try:
                    self.app_server.sessions.interrupt_turn(*active)
                except Exception:
                    pass
            worker = self._worker
            if worker is not None:
                worker.join(timeout=10)
                if worker.is_alive():
                    self.app_server.stop()
                    worker.join(timeout=5)
            self._worker = None
            self.app_server.stop()

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
        finally:
            db.close()
        for turn_id in delivery_ids:
            self._deliver(turn_id)
        return recovered

    def process_next(self) -> bool:
        db = SessionLocal()
        turn_id: int | None = None
        needs_delivery = False
        try:
            turn = db.scalar(
                select(ActTurn).join(ActSession)
                .where(ActTurn.status == "queued", ActSession.agent_id == self.agent_id)
                .order_by(ActTurn.created_at, ActTurn.id)
                .limit(1)
            )
            if turn is None:
                return False
            claimed = db.execute(
                update(ActTurn)
                .where(ActTurn.id == turn.id, ActTurn.status == "queued")
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
                while not self._stop.is_set() and self.process_next():
                    processed = True
            except Exception:
                processed = True
            if not processed:
                self._wake.wait(1.0)
            else:
                self._wake.wait(0.1)
            self._wake.clear()

    def _execute(self, db, turn_id: int) -> None:
        turn = db.get(ActTurn, turn_id)
        if turn is None:
            return
        session = db.get(ActSession, turn.session_id)
        if session is None or session.status != "active":
            self._fail(db, turn, "Act session is no longer active")
            return
        try:
            route = "assessment" if self.agent_id == "assistant" else self.agent_id
            routing = CodexRoutingService(db).resolve(role=route, action=route)
            policy = AgentPolicyService(db).policy(self.agent_id)
            model = policy.model or routing.effective_model
            effort = policy.reasoning_effort or routing.effective_reasoning_effort
            recovered_thread = False
            try:
                if session.codex_thread_id.startswith("pending:"):
                    session.codex_thread_id = self.app_server.start_thread(
                        db, model=model, reasoning_effort=effort, session_id=session.id,
                    )
                    db.commit()
                else:
                    self.app_server.resume_thread(
                        db, session.codex_thread_id, model=model,
                        reasoning_effort=effort, session_id=session.id,
                    )
            except Exception as exc:
                if not is_missing_rollout_error(exc):
                    raise
                session.codex_thread_id = self.app_server.start_thread(
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
                    self._active = (session.codex_thread_id, codex_turn_id)
                db.refresh(turn)
                if turn.cancel_requested_at is not None or self._stop.is_set():
                    self.app_server.sessions.interrupt_turn(
                        session.codex_thread_id,
                        codex_turn_id,
                    )

            input_text = self._turn_input(db, turn, recovered_thread=recovered_thread)
            try:
                result = self.app_server.sessions.run_structured_turn(
                    session.codex_thread_id,
                    input_text,
                    ACT_RESPONSE_SCHEMA,
                    timeout_seconds=900,
                    model=model,
                    reasoning_effort=effort,
                    on_turn_started=on_started,
                )
            except Exception as exc:
                db.refresh(turn)
                if recovered_thread or turn.codex_turn_id is not None or not is_missing_rollout_error(exc):
                    raise
                session.codex_thread_id = self.app_server.start_thread(
                    db,
                    session_id=session.id,
                    model=model,
                    reasoning_effort=effort,
                )
                db.commit()
                recovered_thread = True
                result = self.app_server.sessions.run_structured_turn(
                    session.codex_thread_id,
                    self._turn_input(db, turn, recovered_thread=True),
                    ACT_RESPONSE_SCHEMA,
                    timeout_seconds=900,
                    model=model,
                    reasoning_effort=effort,
                    on_turn_started=on_started,
                )
            db.refresh(turn)
            if turn.cancel_requested_at is not None or self._stop.is_set():
                self._cancelled(db, turn)
            else:
                turn.assistant_message = _response_text(result.output_text)
                turn.activity_json = (
                    ([{"kind": "threadRecovery", "label": "Recovered the Act session"}]
                    if recovered_thread else [])
                    + _activities(result.items)
                )
                turn.status = "succeeded"
                turn.completed_at = datetime.now(UTC)
                session.updated_at = datetime.now(UTC)
                db.commit()
        except Exception as exc:
            db.refresh(turn)
            if turn.cancel_requested_at is not None or self._stop.is_set():
                self._cancelled(db, turn)
            else:
                self._fail(db, turn, str(exc))
        finally:
            try:
                AgentPolicyService(db).revoke(session.id)
                db.commit()
                from app.services.agent_proposal_service import AgentProposalService
                AgentProposalService(db).refresh_execution(session.id)
                if self.agent_id == "assistant":
                    if session.origin == "assessment" and turn.id == db.scalar(
                        select(ActTurn.id).where(ActTurn.session_id == session.id).order_by(ActTurn.id).limit(1)
                    ):
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
                    self._active = None

    @staticmethod
    def _turn_input(db, turn: ActTurn, *, recovered_thread: bool) -> str:
        if not recovered_thread:
            return turn.user_message
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
            return turn.user_message
        history = "\n\n".join(
            f"User: {item.user_message}\nAct: {item.assistant_message or ''}"
            for item in completed[-20:]
        )
        return (
            "The previous Codex rollout is unavailable. The following is historical "
            "conversation context only. Do not repeat its actions or tool calls. Continue "
            "from the current request.\n\n"
            f"<historical_conversation>\n{history}\n</historical_conversation>\n\n"
            f"Current user request: {turn.user_message}"
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
