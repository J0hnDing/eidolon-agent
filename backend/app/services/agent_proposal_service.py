from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ActTurn, AgentProposal, AssistantFollowUp
from app.services.act_workspace_service import ensure_act_workspace


def _write_managed_file(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


class AgentProposalService:
    def __init__(self, db: Session):
        self.db = db

    def list(self) -> list[dict]:
        return [self.serialize(row) for row in self.db.scalars(select(AgentProposal).order_by(AgentProposal.id.desc()))]

    def edit_functions(self, proposal_id: int, steps: list[dict]) -> AgentProposal:
        proposal = self.db.get(AgentProposal, proposal_id)
        if proposal is None or proposal.status != "pending" or not proposal.action_json:
            raise ValueError("Pending function proposal not found")
        if proposal.action_json["type"] != "functions":
            raise ValueError("Only function sequences can be edited")
        if not steps:
            raise ValueError("A function sequence needs at least one step")
        from app.services.assistant_assessment_result_service import (
            deterministic_catalog,
            proposal_fingerprint,
            validate_action,
        )

        action = {**proposal.action_json, "steps": steps}
        validate_action(action, deterministic_catalog(self.db), proposal.follow_up_json)
        fingerprint = proposal_fingerprint(action, proposal.provenance_json or {})
        existing = self.db.scalar(select(AgentProposal.id).where(
            AgentProposal.fingerprint == fingerprint, AgentProposal.id != proposal_id,
        ))
        if existing is not None:
            raise ValueError("An identical proposal already exists")
        claimed = self.db.execute(update(AgentProposal).where(
            AgentProposal.id == proposal_id, AgentProposal.status == "pending",
        ).values(action_json=action, fingerprint=fingerprint))
        if not claimed.rowcount:
            self.db.rollback()
            raise ValueError("Proposal was already decided")
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise ValueError("An identical proposal already exists") from None
        self.db.refresh(proposal)
        self.mirror(proposal)
        return proposal

    def decide(self, proposal_id: int, approve: bool) -> AgentProposal:
        proposal = self.db.get(AgentProposal, proposal_id)
        if proposal is None:
            raise ValueError("Proposal not found")
        if proposal.status != "pending":
            return proposal
        action = proposal.action_json or {
            "type": "act", "description": proposal.actions, "instruction": proposal.instruction,
        }
        if approve and action["type"] == "functions":
            from app.services.assistant_assessment_result_service import deterministic_catalog, validate_action

            validate_action(action, deterministic_catalog(self.db), proposal.follow_up_json)
        claimed = self.db.execute(
            update(AgentProposal)
            .where(AgentProposal.id == proposal_id, AgentProposal.status == "pending",
                   AgentProposal.fingerprint == proposal.fingerprint)
            .values(status="approved" if approve else "denied")
        )
        if not claimed.rowcount:
            self.db.rollback()
            current = self.db.get(AgentProposal, proposal_id)
            if current is not None and current.status == "pending":
                raise ValueError("Proposal changed; review the current sequence before approving")
            return current
        try:
            if approve:
                if action["type"] == "act":
                    from app.services.act_session_service import ActSessionService

                    service = ActSessionService(self.db, agent_id="act")
                    session = service.create_session(origin="plan_approval", commit=False)
                    turn = service.enqueue_turn(session.id, action["instruction"], commit=False)
                    proposal.act_session_id = session.id
                    proposal.act_turn_id = turn.id
                proposal.execution_status = "queued"
                if proposal.follow_up_json is not None:
                    timing = proposal.follow_up_json["timing"]
                    due_at = (datetime.fromisoformat(timing["at"].replace("Z", "+00:00"))
                              if timing["type"] == "absolute" else None)
                    self.db.add(AssistantFollowUp(
                        proposal_id=proposal.id, session_id=proposal.source_session_id,
                        message=proposal.follow_up_json["message"], timing_json=timing,
                        due_at=due_at, status="waiting",
                    ))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(proposal)
        if approve and action["type"] == "act":
            service.synchronize_telegram(session)
        self.mirror(proposal)
        self._telegram(proposal)
        if approve:
            if action["type"] == "functions":
                from app.services.assistant_action_service import dispatch_function_proposal

                dispatch_function_proposal(proposal.id)
            else:
                from app.services.act_turn_dispatcher import act_turn_dispatcher

                act_turn_dispatcher.notify()
        return proposal

    def refresh_execution(self, session_id: int | None = None) -> None:
        query = select(AgentProposal).where(AgentProposal.act_turn_id.is_not(None))
        if session_id is not None:
            query = query.where(AgentProposal.act_session_id == session_id)
        for proposal in list(self.db.scalars(query)):
            turn = self.db.get(ActTurn, proposal.act_turn_id)
            if turn is not None and proposal.execution_status != turn.status:
                proposal.execution_status = turn.status
                follow_up = self.db.scalar(select(AssistantFollowUp).where(
                    AssistantFollowUp.proposal_id == proposal.id,
                ))
                if follow_up is not None:
                    if turn.status == "succeeded":
                        follow_up.status = "scheduled"
                    elif turn.status in {"failed", "cancelled", "interrupted"}:
                        follow_up.status = "cancelled"
                self.db.commit()
                self.mirror(proposal)
                self._telegram(proposal)

    def reconcile(self) -> None:
        self.refresh_execution()
        for proposal in self.db.scalars(select(AgentProposal)):
            self.mirror(proposal)

    def mirror(self, proposal: AgentProposal) -> None:
        directory = ensure_act_workspace().knowledge / "assistant" / "plans"
        directory.mkdir(parents=True, exist_ok=True)
        # ID is backend assigned. Files are a regenerable projection, never authority.
        _write_managed_file(
            directory / f"{proposal.id}.json",
            json.dumps(self.serialize(proposal), indent=2, ensure_ascii=False, default=str) + "\n",
        )

    def _telegram(self, proposal: AgentProposal, *, initial: bool = False) -> None:
        from app.services.telegram_service import TelegramService

        try:
            telegram = TelegramService(self.db, role="assistant_agent")
            if initial:
                telegram.send_agent_proposal(proposal)
            else:
                telegram.update_agent_proposal(proposal)
        except Exception:
            # Durable local proposals remain decidable while Telegram is disconnected.
            self.db.rollback()

    @staticmethod
    def serialize(proposal: AgentProposal) -> dict:
        return {
            "id": proposal.id,
            "source_session_id": proposal.source_session_id,
            "title": proposal.title,
            "rationale": proposal.rationale,
            "instruction": proposal.instruction,
            "actions": proposal.actions,
            "action": proposal.action_json,
            "provenance": proposal.provenance_json,
            "follow_up": proposal.follow_up_json,
            "execution_state": proposal.execution_state_json,
            "references": proposal.references_json,
            "status": proposal.status,
            "execution_status": proposal.execution_status,
            "act_session_id": proposal.act_session_id,
            "created_at": proposal.created_at,
            "replaces_proposal_id": proposal.replaces_proposal_id,
            "material_change": proposal.material_change,
        }
