from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Skill, SkillRun, SkillSchedule
from app.schemas.schedule import ScheduleCreate, ScheduleRead, ScheduleWithApproval
from app.schemas.skill_run import SkillRunRead
from app.services.scheduler_service import ScheduleError, SchedulerService, serialize_schedule


router = APIRouter(prefix="/schedules", tags=["schedules"])


@router.get("", response_model=list[ScheduleRead])
def list_schedules(skill_id: int | None = None, db: Session = Depends(get_db)) -> list[dict]:
    query = select(SkillSchedule).order_by(SkillSchedule.created_at.desc())
    if skill_id is not None:
        query = query.where(SkillSchedule.skill_id == skill_id)
    return [serialize_schedule(schedule) for schedule in db.scalars(query).all()]


@router.get("/{schedule_id}", response_model=ScheduleRead)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    schedule = db.get(SkillSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return serialize_schedule(schedule)


@router.post("/{schedule_id}/approve", response_model=ScheduleRead)
def approve_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    try:
        updated = SchedulerService(db).approve_schedule(schedule)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return serialize_schedule(updated)


@router.post("/{schedule_id}/deny", response_model=ScheduleRead)
def deny_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    updated = SchedulerService(db).deny_schedule(schedule)
    return serialize_schedule(updated)


@router.post("/{schedule_id}/pause", response_model=ScheduleRead)
def pause_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    try:
        updated = SchedulerService(db).pause_schedule(schedule)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return serialize_schedule(updated)


@router.post("/{schedule_id}/resume", response_model=ScheduleRead)
def resume_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict:
    schedule = get_schedule_or_404(schedule_id, db)
    try:
        updated = SchedulerService(db).resume_schedule(schedule)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return serialize_schedule(updated)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)) -> Response:
    schedule = get_schedule_or_404(schedule_id, db)
    SchedulerService(db).delete_schedule(schedule)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{schedule_id}/run-now", response_model=SkillRunRead)
def run_schedule_now(schedule_id: int, db: Session = Depends(get_db)) -> SkillRun:
    schedule = get_schedule_or_404(schedule_id, db)
    if schedule.status == "paused":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Paused schedules cannot run")
    return SchedulerService(db).run_scheduled_skill(schedule)


def create_skill_schedule(skill_id: int, payload: ScheduleCreate, db: Session) -> ScheduleWithApproval:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        schedule, approval = SchedulerService(db).create_schedule(skill, payload)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ScheduleWithApproval(
        schedule=ScheduleRead(**serialize_schedule(schedule)),
        approval_request_id=approval.id,
    )


def create_manifest_schedule(skill_id: int, db: Session) -> ScheduleWithApproval:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        result = SchedulerService(db).create_from_manifest_if_present(skill)
    except ScheduleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill manifest does not declare a schedule")
    schedule, approval = result
    return ScheduleWithApproval(
        schedule=ScheduleRead(**serialize_schedule(schedule)),
        approval_request_id=approval.id,
    )


def get_schedule_or_404(schedule_id: int, db: Session) -> SkillSchedule:
    schedule = db.get(SkillSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return schedule
