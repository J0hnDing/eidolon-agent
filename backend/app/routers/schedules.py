from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SkillRun, SkillSchedule
from app.schemas.schedule import ScheduleRead, ScheduleUpdate
from app.schemas.skill_run import SkillRunRead
from app.services.scheduler_service import ScheduleError, SchedulerService, serialize_schedule

router = APIRouter(prefix="/schedules", tags=["schedules"])


def get_scheduler_service(request: Request, db: Session = Depends(get_db)) -> SchedulerService:
    lifespan_service = getattr(request.app.state, "scheduler_service", None)
    scheduler = getattr(lifespan_service, "scheduler", None)
    return SchedulerService(db, scheduler=scheduler)


@router.get("", response_model=list[ScheduleRead])
def list_schedules(
    request: Request,
    skill_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(SkillSchedule).order_by(SkillSchedule.created_at.desc())
    if skill_id is not None:
        query = query.where(SkillSchedule.skill_id == skill_id)
    serialized = [serialize_schedule(schedule) for schedule in db.scalars(query).all()]
    if skill_id is None:
        lifespan_service = getattr(request.app.state, "scheduler_service", None)
        if isinstance(lifespan_service, SchedulerService):
            serialized.insert(0, lifespan_service.serialize_notion_done_cleanup_schedule())
    return serialized


@router.get("/{schedule_id}", response_model=ScheduleRead)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    return serialize_schedule(get_schedule_or_404(schedule_id, db))


@router.put("/{schedule_id}", response_model=ScheduleRead)
def update_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    db: Session = Depends(get_db),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    try:
        updated = scheduler_service.update_schedule(schedule, payload)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return serialize_schedule(updated)


@router.post("/{schedule_id}/pause", response_model=ScheduleRead)
def pause_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    try:
        updated = scheduler_service.pause_schedule(schedule)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return serialize_schedule(updated)


@router.post("/{schedule_id}/resume", response_model=ScheduleRead)
def resume_schedule(
    schedule_id: int,
    db: Session = Depends(get_db),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    try:
        updated = scheduler_service.resume_schedule(schedule)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return serialize_schedule(updated)


@router.post("/{schedule_id}/run-now", response_model=SkillRunRead)
def run_schedule_now(
    schedule_id: int,
    db: Session = Depends(get_db),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> SkillRun:
    schedule = get_schedule_or_404(schedule_id, db)
    try:
        return scheduler_service.run_scheduled_service(schedule)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def get_schedule_or_404(schedule_id: int, db: Session) -> SkillSchedule:
    schedule = db.get(SkillSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return schedule
