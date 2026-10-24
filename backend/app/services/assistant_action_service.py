"""Durable execution of approved Assistant function actions."""

from __future__ import annotations

import json
import threading

from sqlalchemy import select, update

from app.db import SessionLocal
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationTargetRef
from app.models import AgentProposal
from app.services.assistant_actions import deterministic_catalog, validate_action


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
            validate_action(action, catalog)
            context = InvocationContextFactory.trusted_system(
                "assistant_approved_action", initiating_action=f"assistant_proposal_{proposal_id}",
            )
            executor = InvocationExecutor(db)
            for index, step in enumerate(action["steps"]):
                entry = catalog[step["function"]]
                outcome = executor.execute(
                    InvocationTargetRef(category=entry["category"], target_id=step["function"]),
                    step["arguments"], context,
                )
                if outcome.status != "succeeded":
                    raise RuntimeError(f"Function step {index + 1} ended with {outcome.status}")
                state = proposal.execution_state_json or {}
                results = list(state.get("results", []))
                results.append({"function": step["function"],
                                "output": json.dumps(outcome.output, ensure_ascii=False, default=str)[:6000]})
                proposal.execution_state_json = {**state, "completed_steps": index + 1, "results": results}
                db.commit()
            proposal.execution_status = "succeeded"
            db.commit()
        except Exception as exc:
            db.rollback()
            proposal = db.get(AgentProposal, proposal_id)
            proposal.execution_status = "failed"
            state = proposal.execution_state_json or {}
            proposal.execution_state_json = {**state, "error": str(exc)[:512]}
            db.commit()
        from app.services.agent_proposal_service import AgentProposalService

        service = AgentProposalService(db)
        service.mirror(proposal)
        service._telegram(proposal)
        from app.services.assistant_activation_service import AssistantActivationService
        AssistantActivationService(db).tick()
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
