from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Skill, SkillRun
from app.schemas.approval_request import ApprovalRequestRead
from app.schemas.proposed_skill import (
    ProposedSampleCreate,
    ProposedSkillValidationRead,
    SkillFileRead,
)
from app.schemas.skill import SkillCreate, SkillRead, SkillUpdate
from app.schemas.skill_run import SkillRunRead, SkillRunRequest
from app.services.permission_service import PermissionError, PermissionService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.skill_runner import SkillRunner


router = APIRouter(prefix="/skills", tags=["skills"])
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@router.post("", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_skill(payload: SkillCreate, db: Session = Depends(get_db)) -> Skill:
    skill = Skill(**payload.model_dump())
    db.add(skill)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill name already exists") from exc
    db.refresh(skill)
    return skill


@router.get("", response_model=list[SkillRead])
def list_skills(db: Session = Depends(get_db)) -> list[Skill]:
    ProposedSkillService(db).sync_installed_from_filesystem()
    return list(
        db.scalars(
            select(Skill)
            .where(Skill.status != "deleted")
            .order_by(Skill.created_at.desc())
        ).all()
    )


@router.post("/proposed/sample", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_sample_proposed_skill(
    payload: ProposedSampleCreate,
    db: Session = Depends(get_db),
) -> Skill:
    try:
        return ProposedSkillService(db).create_sample(payload.name, payload.skill_type)
    except ProposedSkillError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/proposed", response_model=list[SkillRead])
def list_proposed_skills(db: Session = Depends(get_db)) -> list[Skill]:
    return ProposedSkillService(db).list_proposed()


@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(skill_id: int, db: Session = Depends(get_db)) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return skill


@router.post("/{skill_id}/run", response_model=SkillRunRead)
def run_skill(
    skill_id: int,
    payload: SkillRunRequest,
    db: Session = Depends(get_db),
) -> SkillRun:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    if skill.status != "installed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only installed skills can be run")
    if not skill.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill is disabled")
    if skill.skill_type == "instruction":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Instruction skills cannot be run")
    permission_decision = PermissionService(db).can_run(skill)
    if not permission_decision.allowed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=permission_decision.reason)

    skill_dir = resolve_skill_dir(skill)
    return SkillRunner(db).run(skill_id=skill.id, skill_dir=skill_dir, input_json=payload.input)


@router.get("/{skill_id}/runs", response_model=list[SkillRunRead])
def list_skill_runs(skill_id: int, db: Session = Depends(get_db)) -> list[SkillRun]:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    return list(
        db.scalars(
            select(SkillRun)
            .where(SkillRun.skill_id == skill_id)
            .order_by(SkillRun.started_at.desc(), SkillRun.id.desc())
        ).all()
    )


@router.get("/{skill_id}/files", response_model=list[SkillFileRead])
def list_skill_files(skill_id: int, db: Session = Depends(get_db)) -> list[SkillFileRead]:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        return ProposedSkillService(db).read_skill_files(skill)
    except ProposedSkillError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/validate", response_model=ProposedSkillValidationRead)
def validate_skill(skill_id: int, db: Session = Depends(get_db)) -> ProposedSkillValidationRead:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return ProposedSkillService(db).validate_proposed_skill(skill)


@router.post("/{skill_id}/runtime-permissions/analyze", response_model=ApprovalRequestRead)
def analyze_runtime_permissions(skill_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        return PermissionService(db).create_runtime_request(skill)
    except (PermissionError, ProposedSkillError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/runtime-permissions/approve", response_model=ApprovalRequestRead)
def approve_runtime_permissions(skill_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    permission_service = PermissionService(db)
    request = permission_service.create_runtime_request(skill)
    try:
        return permission_service.approve_request(request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/runtime-permissions/deny", response_model=ApprovalRequestRead)
def deny_runtime_permissions(skill_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    permission_service = PermissionService(db)
    request = permission_service.create_runtime_request(skill)
    try:
        return permission_service.deny_request(request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/install", response_model=SkillRead)
def install_skill(skill_id: int, db: Session = Depends(get_db)) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    permission_decision = PermissionService(db).can_install(skill)
    if not permission_decision.allowed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=permission_decision.reason)
    try:
        return ProposedSkillService(db).install_proposed_skill(skill)
    except ProposedSkillError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_skill(skill_id: int, db: Session = Depends(get_db)) -> Response:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        ProposedSkillService(db).reject_proposed_skill(skill)
    except ProposedSkillError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{skill_id}", response_model=SkillRead)
def update_skill(skill_id: int, payload: SkillUpdate, db: Session = Depends(get_db)) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(skill, key, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill name already exists") from exc
    db.refresh(skill)
    return skill


def resolve_skill_dir(skill: Skill) -> Path:
    raw_path = skill.installed_path or skill.manifest_path
    path = Path(raw_path)
    if path.is_absolute():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill paths must be relative to the project root",
        )
    path = (PROJECT_ROOT / path).resolve()
    if path.name == "manifest.json":
        path = path.parent
    installed_root = (PROJECT_ROOT / "skills" / "installed").resolve()
    if not path.is_relative_to(installed_root):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Executable skills must live under skills/installed",
        )
    return path


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(skill_id: int, db: Session = Depends(get_db)) -> Response:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    try:
        ProposedSkillService(db).delete_skill(skill)
    except ProposedSkillError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
