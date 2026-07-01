from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Skill, SkillRun, SkillVersion
from app.schemas.approval_request import ApprovalRequestRead
from app.schemas.agent_run import AgentRunRead
from app.schemas.proposed_skill import (
    ProposedSampleCreate,
    ProposedSkillValidationRead,
    SkillFileRead,
)
from app.schemas.runner import RunnerStatusRead
from app.schemas.schedule import ScheduleCreate, ScheduleWithApproval
from app.schemas.skill import SkillCreate, SkillRead, SkillUpdate
from app.schemas.skill_run import SkillRunRead, SkillRunRequest
from app.schemas.skill_version import SkillUpdateResponse, SkillUpdateSuggestion, SkillVersionComparison, SkillVersionRead
from app.services.permission_service import PermissionError, PermissionService
from app.services.agent_workflow_service import AgentWorkflowError, AgentWorkflowService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import get_runner_status, get_skill_runner
from app.services.skill_version_service import SkillVersionError, SkillVersionService
from app.routers.schedules import create_manifest_schedule, create_skill_schedule


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


@router.get("/runner-status", response_model=RunnerStatusRead)
def read_runner_status() -> RunnerStatusRead:
    return RunnerStatusRead(**get_runner_status().__dict__)


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
    try:
        with SkillOperationGuard(db).locked(skill, "run", reason="Manual skill run"):
            return get_skill_runner(db).run(skill_id=skill.id, skill_dir=skill_dir, input_json=payload.input)
    except SkillOperationConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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


@router.get("/{skill_id}/versions", response_model=list[SkillVersionRead])
def list_skill_versions(skill_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        return SkillVersionService(db).list_versions(skill)
    except SkillVersionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/versions/update-suggestion", response_model=SkillUpdateResponse)
def suggest_skill_update(
    skill_id: int,
    payload: SkillUpdateSuggestion,
    db: Session = Depends(get_db),
) -> SkillUpdateResponse:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        agent_run = AgentWorkflowService(db).create_update_run(skill, payload.suggestion)
        latest_version = None
        versions = SkillVersionService(db).list_versions(skill) if skill.status == "installed" else []
        for version in versions:
            if version.status in {"draft", "proposed_update"}:
                latest_version = version
                break
        return SkillUpdateResponse(
            agent_run_id=agent_run.id,
            version=latest_version,
            status=agent_run.status,
            message=agent_run.summary or agent_run.error_message or "Update workflow started.",
        )
    except (SkillVersionError, AgentWorkflowError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/versions/{version_id}/activate", response_model=SkillRead)
def activate_skill_version(skill_id: int, version_id: int, db: Session = Depends(get_db)) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    version_service = SkillVersionService(db)
    version = db.get(SkillVersion, version_id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill version not found")
    try:
        return version_service.activate_version(skill, version)
    except SkillVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{skill_id}/versions/{version_id}/discard", status_code=status.HTTP_204_NO_CONTENT)
def discard_skill_version(skill_id: int, version_id: int, db: Session = Depends(get_db)) -> Response:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    version = db.get(SkillVersion, version_id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill version not found")
    try:
        SkillVersionService(db).discard_version(skill, version)
    except SkillVersionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{skill_id}/versions/{version_id}/compare", response_model=SkillVersionComparison)
def compare_skill_version(skill_id: int, version_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    version = db.get(SkillVersion, version_id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill version not found")
    try:
        return SkillVersionService(db).compare_with_active(skill, version)
    except SkillVersionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/schedules", response_model=ScheduleWithApproval, status_code=status.HTTP_201_CREATED)
def create_schedule_for_skill(
    skill_id: int,
    payload: ScheduleCreate,
    db: Session = Depends(get_db),
) -> ScheduleWithApproval:
    return create_skill_schedule(skill_id, payload, db)


@router.post("/{skill_id}/schedules/from-manifest", response_model=ScheduleWithApproval, status_code=status.HTTP_201_CREATED)
def create_manifest_schedule_for_skill(
    skill_id: int,
    db: Session = Depends(get_db),
) -> ScheduleWithApproval:
    return create_manifest_schedule(skill_id, db)


@router.post("/{skill_id}/repair", response_model=AgentRunRead, status_code=status.HTTP_201_CREATED)
def repair_skill_with_agents(skill_id: int, db: Session = Depends(get_db)) -> AgentRunRead:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        return AgentWorkflowService(db).create_repair_run(skill)
    except SkillOperationConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AgentWorkflowError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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
