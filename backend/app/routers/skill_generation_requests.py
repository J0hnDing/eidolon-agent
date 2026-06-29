from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SkillGenerationRequest
from app.schemas.skill_generation import (
    SkillGenerationApprovalResponse,
    SkillGenerationRequestRead,
)
from app.services.codex_service import CodexGenerationError, CodexService
from app.services.permission_service import PermissionError, PermissionService


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
    permission_service = PermissionService(db)
    permission_request = permission_service.create_build_time_request(generation_request)
    try:
        permission_request = permission_service.approve_request(permission_request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    generation_request.status = "approved"
    db.commit()
    try:
        skill, validation = CodexService(db).generate_from_request(generation_request)
    except CodexGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.refresh(generation_request)
    return SkillGenerationApprovalResponse(
        generation_request=generation_request,
        permission_request=permission_request,
        proposed_skill=skill,
        validation=validation,
    )


@router.post("/{request_id}/deny-generation", response_model=SkillGenerationRequestRead)
@router.post("/{request_id}/deny", response_model=SkillGenerationRequestRead)
def deny_generation_request(request_id: int, db: Session = Depends(get_db)) -> SkillGenerationRequest:
    generation_request = db.get(SkillGenerationRequest, request_id)
    if generation_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Generation request not found")
    permission_service = PermissionService(db)
    permission_request = permission_service.create_build_time_request(generation_request)
    try:
        permission_service.deny_request(permission_request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    generation_request.status = "cancelled"
    db.commit()
    db.refresh(generation_request)
    return generation_request
