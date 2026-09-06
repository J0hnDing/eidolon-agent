from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SkillRun, SkillSchedule
from app.schemas.schedule import ScheduleAvailabilityUpdate, ScheduleRead, ScheduleUpdate
from app.schemas.skill_run import SkillRunRead
from app.services.runtime_state_service import RuntimeStateService
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
    running_ids = RuntimeStateService(db).running_schedule_ids()
    serialized = [
        serialize_schedule(schedule, is_running=schedule.id in running_ids)
        for schedule in db.scalars(query).all()
    ]
    if skill_id is None:
        lifespan_service = getattr(request.app.state, "scheduler_service", None)
        if isinstance(lifespan_service, SchedulerService):
            serialized[0:0] = lifespan_service.serialize_platform_schedules()
    return serialized


@router.put("/platform/{service_id}", response_model=ScheduleRead)
def update_platform_schedule(
    service_id: str,
    payload: ScheduleUpdate,
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> dict:
    try:
        return scheduler_service.update_platform_schedule(service_id, payload)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/platform/{service_id}/availability", response_model=ScheduleRead)
def configure_platform_schedule(
    service_id: str,
    payload: ScheduleAvailabilityUpdate,
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> dict:
    try:
        return scheduler_service.configure_platform_service(service_id, payload.enabled)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/platform/{service_id}/run-now")
def run_platform_schedule_now(
    service_id: str,
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> dict:
    try:
        return scheduler_service.run_platform_service_now(service_id)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{schedule_id}", response_model=ScheduleRead)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    return serialize_schedule(
        schedule,
        is_running=schedule.id in RuntimeStateService(db).running_schedule_ids(),
    )


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
