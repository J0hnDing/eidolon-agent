from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.act import ActSessionCreate, ActSessionRead, ActSessionSummary, ActTurnCreate, ActTurnRead
from app.services.act_session_service import ActSessionError, ActSessionService
from app.services.act_workspace_service import ActWorkspaceError, open_act_root

router = APIRouter(prefix="/act", tags=["act"])


def _error(exc: ActSessionError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/workspace/open-root", status_code=status.HTTP_204_NO_CONTENT)
def open_workspace_root() -> Response:
    try:
        open_act_root()
    except ActWorkspaceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions", response_model=list[ActSessionSummary])
def list_sessions(db: Session = Depends(get_db)):
    return ActSessionService(db).list_sessions()


@router.post("/sessions", response_model=ActSessionRead, status_code=status.HTTP_201_CREATED)
def create_session(payload: ActSessionCreate, db: Session = Depends(get_db)):
    try:
        return ActSessionService(db).create_session(origin=payload.origin)
    except ActSessionError as exc:
        raise _error(exc) from None


@router.get("/sessions/{session_id}", response_model=ActSessionRead)
def read_session(session_id: int, db: Session = Depends(get_db)):
    try:
        return ActSessionService(db).read_session(session_id)
    except ActSessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.post(
    "/sessions/{session_id}/turns",
    response_model=ActTurnRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_turn(session_id: int, payload: ActTurnCreate, db: Session = Depends(get_db)):
    try:
        return ActSessionService(db).enqueue_turn(session_id, payload.message)
    except ActSessionError as exc:
        raise _error(exc) from None


@router.post("/sessions/{session_id}/turns/{turn_id}/cancel", response_model=ActTurnRead)
def cancel_turn(session_id: int, turn_id: int, db: Session = Depends(get_db)):
    try:
        return ActSessionService(db).cancel_turn(session_id, turn_id)
    except ActSessionError as exc:
        raise _error(exc) from None


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_session(session_id: int, db: Session = Depends(get_db)):
    try:
        ActSessionService(db).archive(session_id)
    except ActSessionError as exc:
        raise _error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
