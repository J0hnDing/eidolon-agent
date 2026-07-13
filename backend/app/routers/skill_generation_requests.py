from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApprovalRequest, SkillGenerationRequest
from app.schemas.agent_run import AgentRunRead
from app.schemas.skill_generation import (
    SkillGenerationApprovalResponse,
    SkillGenerationRequestRead,
)
from app.services.agent_workflow_service import AgentWorkflowError, AgentWorkflowService
from app.services.codex_service import CodexGenerationError
from app.services.permission_service import PermissionError, PermissionService
from app.services.proposed_skill_service import ProposedSkillService

router = APIRouter(prefix="/skill-generation-requests", tags=["skill_generation_requests"])


@router.get("", response_model=list[SkillGenerationRequestRead])
def list_generation_requests(db: Session = Depends(get_db)) -> list[SkillGenerationRequest]:
    return list(db.scalars(select(SkillGenerationRequest).order_by(SkillGenerationRequest.created_at.desc())).all())


@router.get("/{request_id}", response_model=SkillGenerationRequestRead)
def get_generation_request(request_id: int, db: Session = Depends(get_db)) -> SkillGenerationRequest:
    generation_request = db.get(SkillGenerationRequest, request_id)
    if generation_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation request not found")
    return generation_request


@router.post("/{request_id}/approve-generation", response_model=SkillGenerationApprovalResponse)
@router.post("/{request_id}/approve", response_model=SkillGenerationApprovalResponse)
def approve_generation_request(
    request_id: int,
    db: Session = Depends(get_db),
) -> SkillGenerationApprovalResponse:
    generation_request = db.get(SkillGenerationRequest, request_id)
    if generation_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation request not found")
    if generation_request.status in {"planned", "needs_input"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ProductManager has not finished the build blueprint and permission plan yet.",
        )
    permission_service = PermissionService(db)
    permission_request = permission_service.create_build_time_request(generation_request)
    try:
        permission_request = permission_service.approve_request(permission_request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    generation_request.status = "approved"
    db.commit()
    try:
        agent_run, skill, validation = AgentWorkflowService(db).continue_build_after_approval(generation_request)
    except (AgentWorkflowError, CodexGenerationError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.refresh(generation_request)
    runtime_permission_request = None
    if skill is not None:
        runtime_permission_request = db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.skill_id == skill.id)
            .where(ApprovalRequest.request_scope == "runtime")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        )
    return SkillGenerationApprovalResponse(
        generation_request=generation_request,
        permission_request=permission_request,
        proposed_skill=skill,
        validation=validation,
        agent_run=agent_run,
        runtime_permission_request=runtime_permission_request,
    )


@router.post("/{request_id}/agent-run", response_model=AgentRunRead)
def create_generation_agent_run(request_id: int, db: Session = Depends(get_db)):
    generation_request = db.get(SkillGenerationRequest, request_id)
    if generation_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation request not found")
    return AgentWorkflowService(db).create_build_run(generation_request)


@router.post("/{request_id}/deny-generation", response_model=SkillGenerationRequestRead)
@router.post("/{request_id}/deny", response_model=SkillGenerationRequestRead)
def deny_generation_request(request_id: int, db: Session = Depends(get_db)) -> SkillGenerationRequest:
    generation_request = db.get(SkillGenerationRequest, request_id)
    if generation_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation request not found")
    if generation_request.status in {"planned", "needs_input"}:
        generation_request.status = "cancelled"
        db.commit()
        db.refresh(generation_request)
        return generation_request
    permission_service = PermissionService(db)
    permission_request = permission_service.create_build_time_request(generation_request)
    try:
        permission_service.deny_request(permission_request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if generation_request.proposed_skill is not None and generation_request.proposed_skill.status == "building":
        ProposedSkillService(db).delete_skill(generation_request.proposed_skill)
        generation_request = db.get(SkillGenerationRequest, request_id)
        if generation_request is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation request not found")
    generation_request.status = "cancelled"
    db.commit()
    db.refresh(generation_request)
    return generation_request
