from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.act import ActSessionCreate, ActSessionRead, ActSessionSummary, ActTurnCreate, ActTurnRead
from app.schemas.agents import AgentPolicyUpdate
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.agent_policy_service import AGENTS, AgentPermissionError, AgentPolicyService
from app.services.agent_proposal_service import AgentProposalService
from app.services.assistant_assessment_service import AssistantAssessmentError
from app.services.scheduler_service import SchedulerService

router = APIRouter(prefix="/agents", tags=["agents"])


class AssessmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def service(agent_id: str, db: Session) -> ActSessionService:
    if agent_id not in AGENTS:
        raise HTTPException(404, "Agent not found")
    return ActSessionService(db, agent_id=agent_id)


def scheduler(request: Request, db: Session) -> SchedulerService:
    shared = request.app.state.scheduler_service
    return SchedulerService(
        db, scheduler=shared.scheduler, session_factory=shared.session_factory, project_root=shared.project_root
    )


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


@router.get("/assistant/assessment")
def assessment(request: Request, db: Session = Depends(get_db)):
    return scheduler(request, db).assistant_assessment_status()


@router.put("/assistant/assessment")
def configure_assessment(payload: AssessmentUpdate, request: Request, db: Session = Depends(get_db)):
    return scheduler(request, db).configure_assistant_assessment(payload.enabled)


@router.post("/assistant/assessment/run")
def run_assessment(request: Request, db: Session = Depends(get_db)):
    try:
        runner = scheduler(request, db)
        result = runner.run_assistant_assessment_now()
        return {**runner.assistant_assessment_status(), **result}
    except (ValueError, AssistantAssessmentError) as exc:
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
        return service(agent_id, db).create_session(origin=payload.origin)
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/{agent_id}/sessions/{session_id}", response_model=ActSessionRead)
def read_session(agent_id: str, session_id: int, db: Session = Depends(get_db)):
    try:
        return service(agent_id, db).read_session(session_id)
    except ActSessionError as exc:
        raise HTTPException(404, str(exc)) from None


@router.post("/{agent_id}/sessions/{session_id}/turns", response_model=ActTurnRead, status_code=202)
def enqueue(agent_id: str, session_id: int, payload: ActTurnCreate, db: Session = Depends(get_db)):
    try:
        return service(agent_id, db).enqueue_turn(session_id, payload.message)
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{agent_id}/sessions/{session_id}/turns/{turn_id}/cancel", response_model=ActTurnRead)
def cancel(agent_id: str, session_id: int, turn_id: int, db: Session = Depends(get_db)):
    try:
        return service(agent_id, db).cancel_turn(session_id, turn_id)
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
def archive(agent_id: str, session_id: int, db: Session = Depends(get_db)):
    try:
        service(agent_id, db).archive(session_id)
        return Response(status_code=204)
    except ActSessionError as exc:
        raise HTTPException(409, str(exc)) from None
