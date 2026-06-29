from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApprovalRequest
from app.schemas.approval_request import ApprovalDecision, ApprovalRequestRead
from app.services.permission_service import PermissionError, PermissionService


router = APIRouter(prefix="/permission-requests", tags=["permission_requests"])


@router.get("", response_model=list[ApprovalRequestRead])
def list_permission_requests(
    status_filter: str | None = Query(default=None, alias="status"),
    request_scope: str | None = None,
    skill_id: int | None = None,
    generation_request_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[ApprovalRequest]:
    query = select(ApprovalRequest)
    if status_filter is not None:
        query = query.where(ApprovalRequest.status == status_filter)
    if request_scope is not None:
        query = query.where(ApprovalRequest.request_scope == request_scope)
    if skill_id is not None:
        query = query.where(ApprovalRequest.skill_id == skill_id)
    if generation_request_id is not None:
        query = query.where(ApprovalRequest.generation_request_id == generation_request_id)
    return list(db.scalars(query.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())).all())


@router.get("/{request_id}", response_model=ApprovalRequestRead)
def get_permission_request(request_id: int, db: Session = Depends(get_db)) -> ApprovalRequest:
    request = db.get(ApprovalRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission request not found")
    return request


@router.post("/{request_id}/approve", response_model=ApprovalRequestRead)
def approve_permission_request(
    request_id: int,
    payload: ApprovalDecision | None = None,
    db: Session = Depends(get_db),
) -> ApprovalRequest:
    request = db.get(ApprovalRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission request not found")
    try:
        return PermissionService(db).approve_request(request, payload.decision_notes if payload else None)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{request_id}/deny", response_model=ApprovalRequestRead)
def deny_permission_request(
    request_id: int,
    payload: ApprovalDecision | None = None,
    db: Session = Depends(get_db),
) -> ApprovalRequest:
    request = db.get(ApprovalRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission request not found")
    try:
        return PermissionService(db).deny_request(request, payload.decision_notes if payload else None)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
