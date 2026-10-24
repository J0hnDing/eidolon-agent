from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.act import ActSessionCreate, ActSessionRead, ActSessionSummary, ActTurnCreate, ActTurnRead
from app.schemas.agents import AgentPolicyUpdate
from app.schemas.assistant import AgendaCreate, AgendaRun, AgendaUpdate, AssistantSettingsUpdate
from app.schemas.assistant_actions import FunctionStep
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.agent_chat_history import project_session, project_turn
from app.services.agent_policy_service import AGENTS, AgentPermissionError, AgentPolicyService
from app.services.agent_proposal_service import AgentProposalService
from app.services.assistant_activation_service import AssistantActivationService
from app.services.assistant_service import AssistantService

router = APIRouter(prefix="/agents", tags=["agents"])


class FunctionSequenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[FunctionStep]


def service(agent_id: str, db: Session) -> ActSessionService:
    if agent_id not in AGENTS:
        raise HTTPException(404, "Agent not found")
    return ActSessionService(db, agent_id=agent_id)


@router.get("")
def list_agents(db: Session = Depends(get_db)):
    return [AgentPolicyService(db).describe(agent_id) for agent_id in AGENTS]


@router.get("/assistant/proposals")
def proposals(db: Session = Depends(get_db)):
    return AgentProposalService(db).list()


@router.post("/assistant/proposals/{proposal_id}/{decision}")
def decide(proposal_id: int, decision: str, db: Session = Depends(get_db)):
    if decision not in {"approve", "deny"}:
        raise HTTPException(404, "Decision not found")
    try:
        return AgentProposalService.serialize(AgentProposalService(db).decide(proposal_id, decision == "approve"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.patch("/assistant/proposals/{proposal_id}/functions")
def edit_function_proposal(proposal_id: int, payload: FunctionSequenceUpdate, db: Session = Depends(get_db)):
    try:
        return AgentProposalService.serialize(AgentProposalService(db).edit_functions(
            proposal_id, [step.model_dump() for step in payload.steps],
        ))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/assistant/runtime")
def assistant_runtime(db: Session = Depends(get_db)):
    return AssistantActivationService(db).status()


@router.put("/assistant/runtime")
def configure_assistant(payload: AssistantSettingsUpdate, db: Session = Depends(get_db)):
    try:
        return AssistantActivationService(db).configure(payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/assistant/agenda/{item_id}/run")
def run_agenda_item(item_id: int, payload: AgendaRun, db: Session = Depends(get_db)):
    try:
        runner = AssistantActivationService(db)
        return runner.run_now(item_id, payload.expected_revision)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/assistant/agenda/{item_id}/occurrences")
def agenda_occurrences(item_id: int, db: Session = Depends(get_db)):
    from sqlalchemy import select

    from app.models import AssistantAgendaItem, AssistantAgendaOccurrence
    from app.services.assistant_recurrence import serialize_occurrence

    if db.get(AssistantAgendaItem, item_id) is None:
        raise HTTPException(404, "Agenda item not found")
    return [serialize_occurrence(row) for row in db.scalars(select(AssistantAgendaOccurrence).where(
        AssistantAgendaOccurrence.agenda_item_id == item_id,
    ).order_by(AssistantAgendaOccurrence.scheduled_at.desc()).limit(100))]


@router.post("/assistant/runtime/refresh")
def refresh_assistant(db: Session = Depends(get_db)):
    result = AssistantService(db).request_refresh()
    return {**AssistantActivationService(db).status(), **result}


@router.get("/assistant/agenda")
def assistant_agenda(db: Session = Depends(get_db)):
    return AssistantService(db).agenda()


@router.post("/assistant/agenda", status_code=201)
def create_agenda_item(payload: AgendaCreate, db: Session = Depends(get_db)):
    return AssistantService(db).create_item(payload)


@router.patch("/assistant/agenda/{item_id}")
def update_agenda_item(item_id: int, payload: AgendaUpdate, db: Session = Depends(get_db)):
    try:
        return AssistantService(db).update_item(item_id, payload)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("/assistant/agenda/{item_id}", status_code=204)
def delete_agenda_item(item_id: int, expected_revision: int, db: Session = Depends(get_db)):
    try:
        AssistantService(db).delete_item(item_id, expected_revision)
        return Response(status_code=204)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{agent_id}")
def read_agent(agent_id: str, db: Session = Depends(get_db)):
    service(agent_id, db)
    return AgentPolicyService(db).describe(agent_id)


@router.put("/{agent_id}/policy")
def update_policy(agent_id: str, payload: AgentPolicyUpdate, db: Session = Depends(get_db)):
    service(agent_id, db)
    try:
        return AgentPolicyService(db).update(agent_id, payload)
    except AgentPermissionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{agent_id}/sessions", response_model=list[ActSessionSummary])
def sessions(agent_id: str, db: Session = Depends(get_db)):
    return service(agent_id, db).list_sessions()


@router.post("/{agent_id}/sessions", response_model=ActSessionRead, status_code=201)
def create_session(agent_id: str, payload: ActSessionCreate, db: Session = Depends(get_db)):
    try:
        return project_session(db, service(agent_id, db).create_session(origin=payload.origin))
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{agent_id}/sessions/{session_id}", response_model=ActSessionRead)
def read_session(agent_id: str, session_id: int, db: Session = Depends(get_db)):
    try:
        return project_session(db, service(agent_id, db).read_session(session_id))
    except ActSessionError as exc:
        raise HTTPException(404, str(exc)) from None


@router.post("/{agent_id}/sessions/{session_id}/turns", response_model=ActTurnRead, status_code=202)
def enqueue(agent_id: str, session_id: int, payload: ActTurnCreate, db: Session = Depends(get_db)):
    try:
        return project_turn(db, service(agent_id, db).enqueue_turn(session_id, payload.message), agent_id=agent_id)
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{agent_id}/sessions/{session_id}/turns/{turn_id}/cancel", response_model=ActTurnRead)
def cancel(agent_id: str, session_id: int, turn_id: int, db: Session = Depends(get_db)):
    try:
        return project_turn(db, service(agent_id, db).cancel_turn(session_id, turn_id), agent_id=agent_id)
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
def archive(agent_id: str, session_id: int, db: Session = Depends(get_db)):
    try:
        service(agent_id, db).archive(session_id)
        return Response(status_code=204)
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None
