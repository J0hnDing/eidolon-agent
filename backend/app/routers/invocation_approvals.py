from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.invocation_approval import (
    InvocationApprovalDecisionRequest,
    InvocationApprovalRead,
)
from app.services.invocation_approval_service import (
    InvocationApprovalError,
    InvocationApprovalService,
)

router = APIRouter(prefix="/invocation-approvals", tags=["invocation-approvals"])


@router.get("", response_model=list[InvocationApprovalRead])
def list_invocation_approvals(
    decision_status: str | None = Query(default=None),
    execution_status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return InvocationApprovalService(db).list(
        decision_status=decision_status,
        execution_status=execution_status,
    )


@router.get("/{approval_id}", response_model=InvocationApprovalRead)
def get_invocation_approval(approval_id: int, db: Session = Depends(get_db)):
    return _approval_or_http(lambda: InvocationApprovalService(db).get(approval_id))


@router.post("/{approval_id}/approve", response_model=InvocationApprovalRead)
def approve_invocation(
    approval_id: int,
    payload: InvocationApprovalDecisionRequest,
    db: Session = Depends(get_db),
):
    return _approval_or_http(
        lambda: InvocationApprovalService(db).approve(
            approval_id,
            decided_via="local",
            decided_by=payload.decided_by,
        )
    )


@router.post("/{approval_id}/deny", response_model=InvocationApprovalRead)
def deny_invocation(
    approval_id: int,
    payload: InvocationApprovalDecisionRequest,
    db: Session = Depends(get_db),
):
    return _approval_or_http(
        lambda: InvocationApprovalService(db).deny(
            approval_id,
            decided_via="local",
            decided_by=payload.decided_by,
        )
    )


def _approval_or_http(action):
    try:
        return action()
    except InvocationApprovalError as exc:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if exc.error_type == "not_found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
