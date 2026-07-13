from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApprovalRequest, Skill
from app.routers.skills import resolve_skill_dir
from app.schemas.skill_run import SkillRunRequest
from app.schemas.tool import ToolRead, ToolRunResponse
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import get_skill_runner

router = APIRouter(prefix="/tools", tags=["tools"])
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@router.get("", response_model=list[ToolRead])
def list_tools(db: Session = Depends(get_db)) -> list[ToolRead]:
    ProposedSkillService(db).sync_installed_from_filesystem()
    skills = list(
        db.scalars(
            select(Skill)
            .where(Skill.status == "installed")
            .where(Skill.enabled.is_(True))
            .where(Skill.skill_type == "automation")
            .where(Skill.interface_type == "tool")
            .order_by(Skill.name.asc())
        ).all()
    )
    permission_service = PermissionService(db)
    return [serialize_tool(skill, permission_service) for skill in skills]


@router.get("/{skill_id}", response_model=ToolRead)
def get_tool(skill_id: int, db: Session = Depends(get_db)) -> ToolRead:
    ProposedSkillService(db).sync_installed_from_filesystem()
    skill = db.get(Skill, skill_id)
    if skill is None or skill.interface_type != "tool":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    if skill.status != "installed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only installed tools are available")
    if not skill.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tool is disabled")
    if skill.skill_type == "instruction":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Instruction skills cannot be tools")
    return serialize_tool(skill, PermissionService(db))


@router.post("/{skill_id}/run", response_model=ToolRunResponse)
def run_tool(
    skill_id: int,
    payload: SkillRunRequest,
    db: Session = Depends(get_db),
) -> ToolRunResponse:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    if skill.interface_type != "tool":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    if skill.status != "installed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only installed tools can be run")
    if not skill.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tool is disabled")
    if skill.skill_type == "instruction":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Instruction skills cannot be run as tools")

    permission_decision = PermissionService(db).can_run(skill)
    if not permission_decision.allowed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=permission_decision.reason)

    try:
        with SkillOperationGuard(db).locked(skill, "run", reason="Tool run"):
            run = get_skill_runner(db).run(skill_id=skill.id, skill_dir=resolve_skill_dir(skill), input_json=payload.input)
    except SkillOperationConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ToolRunResponse(skill=skill, run=run)


def serialize_tool(skill: Skill, permission_service: PermissionService) -> ToolRead:
    decision = permission_service.can_run(skill)
    return ToolRead(
        skill=skill,
        runtime_permission_status=runtime_permission_status(skill, decision.allowed, permission_service.db),
        runtime_blocked_reason=None if decision.allowed else decision.reason,
    )


def runtime_permission_status(skill: Skill, can_run: bool, db: Session) -> str:
    if can_run:
        return "ready"
    request = db.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.skill_id == skill.id)
        .where(ApprovalRequest.request_scope == "runtime")
        .where(ApprovalRequest.request_type == "install")
        .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
    )
    if request is None:
        return "not_reviewed"
    return request.status
