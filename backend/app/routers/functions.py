from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.models import Skill, SkillRun
from app.schemas.function_registry import (
    FunctionCatalogEntryRead,
    FunctionContractRead,
    FunctionInvocationRequest,
    FunctionInvocationResponse,
)
from app.schemas.skill_codex import SkillCodexRequest, SkillCodexResponse
from app.schemas.skill_run import SkillRunRead
from app.services.function_catalog_service import FunctionCatalogService
from app.services.function_registry_service import FunctionRegistryError, FunctionRegistryService

router = APIRouter(prefix="/functions", tags=["functions"])
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@router.get("/catalog", response_model=list[FunctionCatalogEntryRead])
def list_function_catalog(
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[dict]:
    return FunctionCatalogService(db).list_entries(
        refresh=refresh,
        include_runtime_state=True,
    )


@router.get("", response_model=list[FunctionContractRead])
def list_functions(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> list[FunctionContractRead]:
    service = FunctionRegistryService(db, project_root=PROJECT_ROOT)
    if authorization is None:
        return service.list_contracts()
    try:
        context = InvocationContextFactory(
            db,
            project_root=PROJECT_ROOT,
        ).from_runtime_capability(_bearer_token(authorization))
    except FunctionRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    caller = db.get(Skill, context.caller_skill_id)
    return service.list_contracts(caller)


@router.post(
    "/capabilities/codex",
    response_model=SkillCodexResponse,
    include_in_schema=False,
)
def call_codex_from_capability(
    payload: SkillCodexRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> SkillCodexResponse:
    try:
        context = InvocationContextFactory(
            db,
            project_root=PROJECT_ROOT,
        ).from_runtime_capability(
            _bearer_token(authorization),
            initiating_action="backend.codex.call",
        )
        outcome = InvocationExecutor(db, project_root=PROJECT_ROOT).execute(
            InvocationTargetRef(category="backend_core", target_id="backend.codex.call"),
            payload.model_dump(mode="json"),
            context,
        )
    except FunctionRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except InvocationExecutionError as exc:
        code = (
            status.HTTP_400_BAD_REQUEST
            if exc.error_type == "invalid_input"
            else status.HTTP_502_BAD_GATEWAY
            if exc.error_type == "codex_failed"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return SkillCodexResponse(**(outcome.output or {}))


@router.post("/{function_name}/invoke", response_model=FunctionInvocationResponse)
def invoke_function(
    function_name: str,
    payload: FunctionInvocationRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FunctionInvocationResponse:
    try:
        context = InvocationContextFactory(
            db,
            project_root=PROJECT_ROOT,
        ).from_runtime_capability(
            _bearer_token(authorization),
            initiating_action=None,
        )
        outcome = InvocationExecutor(db, project_root=PROJECT_ROOT).execute(
            InvocationTargetRef(category="user", target_id=function_name),
            payload.input,
            context,
        )
    except FunctionRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvocationExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if outcome.approval_id is not None:
        receipt = {"status": "pending_approval", "approval_id": outcome.approval_id}
        return FunctionInvocationResponse(output=receipt, approval=receipt)
    run = db.get(SkillRun, outcome.skill_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Function run was not recorded")
    return FunctionInvocationResponse(
        run=SkillRunRead.model_validate(run),
        output=run.output_json,
        error=run.error_message,
    )


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer function caller capability is required",
        )
    return token.strip()
