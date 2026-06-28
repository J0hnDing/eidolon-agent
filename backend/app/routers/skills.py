from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Skill, SkillRun
from app.schemas.skill import SkillCreate, SkillRead, SkillUpdate
from app.schemas.skill_run import SkillRunRead, SkillRunRequest
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
    return list(db.scalars(select(Skill).order_by(Skill.created_at.desc())).all())


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
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.name == "manifest.json":
        return path.parent
    return path


@router.delete("/{skill_id}", response_model=SkillRead)
def delete_skill(skill_id: int, db: Session = Depends(get_db)) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    skill.status = "deleted"
    skill.enabled = False
    db.commit()
    db.refresh(skill)
    return skill
