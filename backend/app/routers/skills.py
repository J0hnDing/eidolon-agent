from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApprovalRequest, Skill, SkillRun, SkillSchedule, SkillVersion
from app.routers.schedules import get_scheduler_service
from app.schemas.agent_run import AgentRunRead
from app.schemas.approval_request import ApprovalRequestRead
from app.schemas.proposed_skill import (
    ProposedSkillValidationRead,
    SkillFileRead,
)
from app.schemas.runner import RunnerStatusRead
from app.schemas.skill import SkillRead, SkillUpdate
from app.schemas.skill_codex import SkillCodexRequest, SkillCodexResponse
from app.schemas.skill_run import SkillRunRead, SkillRunRequest
from app.schemas.skill_version import (
    SkillUpdateResponse,
    SkillUpdateSuggestion,
    SkillVersionComparison,
    SkillVersionRead,
)
from app.services.agent_workflow_service import AgentWorkflowError, AgentWorkflowService
from app.services.codex_service import CodexGenerationError
from app.services.function_registry_service import FunctionRegistryService
from app.services.permission_service import PermissionError, PermissionService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.scheduler_service import SchedulerService
from app.services.skill_codex_runtime_service import (
    SkillCodexInvalidRequest,
    SkillCodexRuntimeService,
    SkillCodexUnavailable,
)
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import get_runner_status
from app.services.skill_version_service import SkillVersionError, SkillVersionService

router = APIRouter(prefix="/skills", tags=["skills"])
PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
    if skill.runtime != "function":
        detail = (
            "service skills run only through their required schedule"
            if skill.runtime == "service"
            else "web_app skills are opened as persistent application sessions, not bounded runs"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        )
    if not skill.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill is disabled")
    permission_decision = PermissionService(db).can_run(skill)
    if not permission_decision.allowed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=permission_decision.reason)

    return FunctionRegistryService(db).invoke_direct(
        skill,
        payload.input,
        source="direct_user",
        initiating_action="Manual skill run",
    )


@router.post("/{skill_id}/codex", response_model=SkillCodexResponse)
def call_codex_for_skill(
    skill_id: int,
    payload: SkillCodexRequest,
    db: Session = Depends(get_db),
) -> SkillCodexResponse:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        result = SkillCodexRuntimeService(db, project_root=PROJECT_ROOT).call(
            skill,
            payload,
        )
    except SkillCodexInvalidRequest as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SkillCodexUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CodexGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return SkillCodexResponse(**result)


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
        permission_request = None
        if agent_run.final_summary_json and agent_run.final_summary_json.get("permission_request_id"):
            permission_request = db.get(ApprovalRequest, agent_run.final_summary_json["permission_request_id"])
        return SkillUpdateResponse(
            agent_run_id=agent_run.id,
            version=latest_version,
            permission_request=permission_request,
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
    try:
        return permission_service.approve_runtime_bundle(skill)
    except (PermissionError, ProposedSkillError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/{skill_id}/runtime-permissions/deny", response_model=ApprovalRequestRead)
def deny_runtime_permissions(skill_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    permission_service = PermissionService(db)
    try:
        return permission_service.deny_runtime_bundle(skill)
    except (PermissionError, ProposedSkillError, FileNotFoundError, ValueError) as exc:
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

    if skill.status != "installed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only installed skills can be enabled or disabled")
    if payload.enabled is not None:
        if skill.runtime == "service":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Service activation is controlled by its schedule",
            )
        if skill.enabled and not payload.enabled:
            from app.services.web_app_runtime_service import WebAppRuntimeService

            try:
                with SkillOperationGuard(db).locked(skill, "disable", reason="Disabling installed skill"):
                    WebAppRuntimeService(db).stop_skill_instances(skill, "Skill disabled")
                    skill.enabled = False
            except SkillOperationConflict as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        else:
            skill.enabled = payload.enabled
    db.commit()
    db.refresh(skill)
    from app.services.function_catalog_service import FunctionCatalogService

    FunctionCatalogService(db).refresh()
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
            detail="Skills must live under skills/installed",
        )
    return path


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    skill_id: int,
    db: Session = Depends(get_db),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
) -> Response:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")

    try:
        schedule_ids = list(
            db.scalars(select(SkillSchedule.id).where(SkillSchedule.skill_id == skill.id)).all()
        )
        for schedule_id in schedule_ids:
            scheduler_service.remove_job(schedule_id)
        ProposedSkillService(db).delete_skill(skill)
    except ProposedSkillError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
