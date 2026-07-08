from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import AgentRun, AgentRunStep
from app.schemas.agent_run import AgentRunDetailRead, AgentRunRead, AgentRunStepRead
from app.services.agent_workflow_service import AgentWorkflowError, AgentWorkflowService


router = APIRouter(prefix="/agent-runs", tags=["agent_runs"])


@router.get("", response_model=list[AgentRunRead])
def list_agent_runs(db: Session = Depends(get_db)) -> list[AgentRun]:
    return list(db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc(), AgentRun.id.desc())).all())


@router.get("/{agent_run_id}", response_model=AgentRunDetailRead)
def get_agent_run(agent_run_id: int, db: Session = Depends(get_db)) -> AgentRun:
    agent_run = db.scalar(
        select(AgentRun).options(selectinload(AgentRun.steps)).where(AgentRun.id == agent_run_id)
    )
    if agent_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    agent_run.steps.sort(key=lambda step: step.id)
    return agent_run


@router.get("/{agent_run_id}/steps", response_model=list[AgentRunStepRead])
def list_agent_run_steps(agent_run_id: int, db: Session = Depends(get_db)) -> list[AgentRunStep]:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    return list(
        db.scalars(
            select(AgentRunStep)
            .where(AgentRunStep.agent_run_id == agent_run_id)
            .order_by(AgentRunStep.id.asc())
        ).all()
    )


@router.post("/{agent_run_id}/cancel", response_model=AgentRunRead)
def cancel_agent_run(agent_run_id: int, db: Session = Depends(get_db)) -> AgentRun:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    return AgentWorkflowService(db).cancel_run(agent_run)


@router.delete("/{agent_run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent_run(agent_run_id: int, db: Session = Depends(get_db)) -> None:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    db.delete(agent_run)
    db.commit()


@router.post("/{agent_run_id}/resume", response_model=AgentRunRead)
def resume_agent_run(agent_run_id: int, db: Session = Depends(get_db)) -> AgentRun:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    try:
        return AgentWorkflowService(db).resume_run(agent_run)
    except AgentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{agent_run_id}/retry-current-milestone", response_model=AgentRunRead)
def retry_current_milestone(agent_run_id: int, db: Session = Depends(get_db)) -> AgentRun:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    try:
        return AgentWorkflowService(db).retry_current_milestone(agent_run)
    except AgentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{agent_run_id}/retry-current-task", response_model=AgentRunRead)
def retry_current_task(agent_run_id: int, db: Session = Depends(get_db)) -> AgentRun:
    agent_run = db.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")
    try:
        return AgentWorkflowService(db).retry_current_task(agent_run)
    except AgentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{agent_run_id}/retry-step/{step_id}", response_model=AgentRunRead)
def retry_agent_run_step(agent_run_id: int, step_id: int, db: Session = Depends(get_db)) -> AgentRun:
    agent_run = db.get(AgentRun, agent_run_id)
    step = db.get(AgentRunStep, step_id)
    if agent_run is None or step is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run or step not found")
    try:
        return AgentWorkflowService(db).retry_step(agent_run, step)
    except AgentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
