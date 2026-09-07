from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.models import ActSession, ActTurn, AgentProposal
from app.schemas.agents import AgentPlanRequest
from app.services.act_workspace_service import ensure_act_workspace
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService


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

    def submit(self, context: InvocationContext, arguments: dict) -> AgentProposal:
        if (
            context.principal_kind != "agent"
            or context.agent_id != "assistant"
            or context.agent_session_id is None
        ):
            raise AgentPermissionError("Plan requests require an authenticated Assistant session")
        session_id = context.agent_session_id
        AgentPolicyService(self.db).require_session("assistant", session_id)
        AgentPolicyService(self.db).require_function("assistant", "plan_approval_request")
        request = AgentPlanRequest.model_validate(arguments)
        if (
            request.replaces_proposal_id is not None
            and self.db.get(AgentProposal, request.replaces_proposal_id) is None
        ):
            raise ValueError("The prior proposal does not exist")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "instruction": " ".join(request.instruction.casefold().split()),
                    "references": sorted(request.references),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        existing = self.db.scalar(select(AgentProposal).where(AgentProposal.fingerprint == fingerprint))
        if existing is not None:
            return existing
        if request.replaces_proposal_id is None:
            claimed = self.db.execute(
                update(ActSession)
                .where(
                    ActSession.id == session_id,
                    ActSession.agent_id == "assistant",
                    ActSession.proposal_count < 5,
                )
                .values(proposal_count=ActSession.proposal_count + 1)
            )
            if not claimed.rowcount:
                self.db.rollback()
                raise ValueError("This thread has reached its limit of 5 new proposals. Replacements remain unlimited.")
        proposal = AgentProposal(
            source_session_id=session_id,
            title=request.title,
            rationale=request.rationale,
            instruction=request.instruction,
            actions=request.actions,
            references_json=request.references,
            fingerprint=fingerprint,
            replaces_proposal_id=request.replaces_proposal_id,
            material_change=request.material_change,
        )
        self.db.add(proposal)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.db.scalar(select(AgentProposal).where(AgentProposal.fingerprint == fingerprint))
            if existing is None:
                raise
            return existing
        self.mirror(proposal)
        self._telegram(proposal, initial=True)
        return proposal

    def decide(self, proposal_id: int, approve: bool) -> AgentProposal:
        proposal = self.db.get(AgentProposal, proposal_id)
        if proposal is None:
            raise ValueError("Proposal not found")
        if proposal.status != "pending":
            return proposal
        if approve:
            AgentPolicyService(self.db).require_function("assistant", "plan_approval_request")
        claimed = self.db.execute(
            update(AgentProposal)
            .where(AgentProposal.id == proposal_id, AgentProposal.status == "pending")
            .values(status="approved" if approve else "denied")
        )
        if not claimed.rowcount:
            self.db.rollback()
            return self.db.get(AgentProposal, proposal_id)
        try:
            if approve:
                from app.services.act_session_service import ActSessionService

                service = ActSessionService(self.db, agent_id="act")
                session = service.create_session(origin="plan_approval", commit=False)
                turn = service.enqueue_turn(session.id, proposal.instruction, commit=False)
                proposal.act_session_id = session.id
                proposal.act_turn_id = turn.id
                proposal.execution_status = "queued"
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(proposal)
        self.mirror(proposal)
        self._telegram(proposal)
        if approve:
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
            "references": proposal.references_json,
            "status": proposal.status,
            "execution_status": proposal.execution_status,
            "act_session_id": proposal.act_session_id,
            "created_at": proposal.created_at,
            "replaces_proposal_id": proposal.replaces_proposal_id,
            "material_change": proposal.material_change,
        }
