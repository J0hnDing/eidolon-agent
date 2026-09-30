"""Durable execution and delivery of approved Assistant actions and follow-ups."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from app.db import SessionLocal
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationTargetRef
from app.models import ActTurn, AgentProposal, AssistantFollowUp, TelegramBotConnection
from app.services.assistant_assessment_result_service import deterministic_catalog, validate_action

logger = logging.getLogger(__name__)


def dispatch_function_proposal(proposal_id: int) -> None:
    threading.Thread(target=run_function_proposal, args=(proposal_id,), daemon=True,
                     name=f"assistant-proposal-{proposal_id}").start()


def run_function_proposal(proposal_id: int) -> None:
    db = SessionLocal()
    try:
        proposal = db.get(AgentProposal, proposal_id)
        if proposal is None or proposal.status != "approved" or not proposal.action_json:
            return
        claimed = db.execute(update(AgentProposal).where(
            AgentProposal.id == proposal_id, AgentProposal.execution_status == "queued",
        ).values(execution_status="running", execution_state_json={"completed_steps": 0}))
        db.commit()
        if not claimed.rowcount:
            return
        proposal = db.get(AgentProposal, proposal_id)
        action = proposal.action_json
        try:
            catalog = deterministic_catalog(db)
            validate_action(action, catalog, proposal.follow_up_json)
            context = InvocationContextFactory.trusted_system(
                "assistant_approved_action", initiating_action=f"assistant_proposal_{proposal_id}",
            )
            executor = InvocationExecutor(db)
            calendar_end = None
            follow_up = db.scalar(select(AssistantFollowUp).where(
                AssistantFollowUp.proposal_id == proposal_id,
            ))
            timing = follow_up.timing_json if follow_up is not None else None
            for index, step in enumerate(action["steps"]):
                entry = catalog[step["function"]]
                outcome = executor.execute(
                    InvocationTargetRef(category=entry["category"], target_id=step["function"]),
                    step["arguments"], context,
                )
                if outcome.status != "succeeded":
                    raise RuntimeError(f"Function step {index + 1} ended with {outcome.status}")
                if timing and timing["type"] == "after_calendar_step" and timing["step_index"] == index:
                    output = outcome.output or {}
                    calendar_end = output.get("end", {}).get("date_time")
                    if not isinstance(calendar_end, str):
                        raise RuntimeError("Calendar event end time was not returned")
                proposal.execution_state_json = {"completed_steps": index + 1}
                db.commit()
            if follow_up is not None:
                if timing["type"] == "after_calendar_step":
                    follow_up.due_at = datetime.fromisoformat(
                        calendar_end.replace("Z", "+00:00"),
                    ) + timedelta(minutes=timing["delay_minutes"])
                follow_up.status = "scheduled"
            proposal.execution_status = "succeeded"
            db.commit()
        except Exception as exc:
            db.rollback()
            proposal = db.get(AgentProposal, proposal_id)
            proposal.execution_status = "failed"
            state = proposal.execution_state_json or {}
            proposal.execution_state_json = {**state, "error": str(exc)[:512]}
            follow_up = db.scalar(select(AssistantFollowUp).where(
                AssistantFollowUp.proposal_id == proposal_id,
            ))
            if follow_up is not None:
                follow_up.status = "cancelled"
            db.commit()
        from app.services.agent_proposal_service import AgentProposalService

        service = AgentProposalService(db)
        service.mirror(proposal)
        service._telegram(proposal)
    finally:
        db.close()


def reconcile_function_actions(session_factory=SessionLocal) -> None:
    """Do not replay possibly completed external steps after process interruption."""
    db = session_factory()
    queued_ids = []
    try:
        rows = list(db.scalars(select(AgentProposal).where(
            AgentProposal.execution_status == "running", AgentProposal.act_turn_id.is_(None),
        )))
        for proposal in rows:
            proposal.execution_status = "interrupted"
            follow_up = db.scalar(select(AssistantFollowUp).where(
                AssistantFollowUp.proposal_id == proposal.id,
            ))
            if follow_up is not None:
                follow_up.status = "cancelled"
        db.commit()
        queued_ids = list(db.scalars(select(AgentProposal.id).where(
            AgentProposal.status == "approved",
            AgentProposal.execution_status == "queued",
            AgentProposal.act_turn_id.is_(None),
            AgentProposal.action_json.is_not(None),
        )))
        for proposal in rows:
            from app.services.agent_proposal_service import AgentProposalService

            service = AgentProposalService(db)
            service.mirror(proposal)
            service._telegram(proposal)
    finally:
        db.close()
    for proposal_id in queued_ids:
        dispatch_function_proposal(proposal_id)


def process_due_followups(session_factory=SessionLocal) -> None:
    db = session_factory()
    message_ids = []
    try:
        now = datetime.now(UTC)
        rows = list(db.scalars(select(AssistantFollowUp).where(
            AssistantFollowUp.status == "scheduled",
            AssistantFollowUp.due_at <= now,
        ).order_by(AssistantFollowUp.due_at, AssistantFollowUp.id)))
        for follow_up in rows:
            proposal = db.get(AgentProposal, follow_up.proposal_id)
            if proposal is None or proposal.execution_status != "succeeded":
                follow_up.status = "cancelled"
                continue
            claimed = db.execute(update(AssistantFollowUp).where(
                AssistantFollowUp.id == follow_up.id, AssistantFollowUp.status == "scheduled",
            ).values(status="sent"))
            if not claimed.rowcount:
                continue
            turn = ActTurn(
                session_id=follow_up.session_id, user_message="",
                assistant_message=follow_up.message, backend_message_kind="follow_up",
                status="succeeded", activity_json=[], created_at=now, completed_at=now,
            )
            db.add(turn)
            db.flush()
            follow_up.sent_turn_id = turn.id
            message_ids.append(turn.id)
        db.commit()
    finally:
        db.close()
    for message_id in message_ids:
        deliver_backend_message(message_id, session_factory=session_factory)


def deliver_backend_message(turn_id: int, *, session_factory=SessionLocal) -> None:
    db = session_factory()
    try:
        turn = db.get(ActTurn, turn_id)
        if turn is None or not turn.backend_message_kind or turn.delivery_status == "delivered":
            return
        connection = db.scalar(select(TelegramBotConnection).where(
            TelegramBotConnection.role == "assistant_agent",
            TelegramBotConnection.is_default.is_(True),
            TelegramBotConnection.status == "connected",
        ))
        if connection is None or not connection.paired_chat_id:
            return
        from app.services.telegram_service import TelegramService

        topic = TelegramService(db, role="assistant_agent").create_topic_for_session(turn.session_id)
        turn.delivery_provider = "telegram"
        turn.delivery_connection_id = topic.connection_id
        turn.delivery_chat_id = topic.telegram_chat_id
        turn.delivery_message_thread_id = topic.message_thread_id
        turn.delivery_status = "pending"
        db.commit()
    except Exception:
        logger.exception("Assistant backend message delivery setup failed")
        db.rollback()
        return
    finally:
        db.close()
    from app.services.agent_turn_delivery import deliver_agent_turn_result

    deliver_agent_turn_result(turn_id, session_factory=session_factory)
    db = session_factory()
    try:
        delivered = db.get(ActTurn, turn_id)
        if delivered is not None and delivered.delivery_status == "delivered":
            follow_up = db.scalar(select(AssistantFollowUp).where(
                AssistantFollowUp.sent_turn_id == turn_id,
            ))
            if follow_up is not None:
                follow_up.status = "delivered"
                db.commit()
    finally:
        db.close()


def retry_backend_messages(session_factory=SessionLocal) -> None:
    db = session_factory()
    try:
        ids = list(db.scalars(select(ActTurn.id).where(
            ActTurn.backend_message_kind.is_not(None),
            or_(ActTurn.delivery_status.is_(None), ActTurn.delivery_status != "delivered"),
        ).order_by(ActTurn.id)))
    finally:
        db.close()
    for turn_id in ids:
        deliver_backend_message(turn_id, session_factory=session_factory)
