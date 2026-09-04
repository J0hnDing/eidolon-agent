from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import InvocationApproval
from app.schemas.function_registry import (
    FunctionCatalogEntryRead,
    FunctionContractRead,
    FunctionInvocationRequest,
    FunctionInvocationResponse,
)
from app.schemas.skill_codex import SkillCodexRequest, SkillCodexResponse
from app.schemas.skill_run import SkillRunRead
from app.services.codex_service import CodexGenerationError
from app.services.function_catalog_service import FunctionCatalogService
from app.services.function_registry_service import FunctionRegistryError, FunctionRegistryService
from app.services.skill_codex_runtime_service import (
    SkillCodexInvalidRequest,
    SkillCodexRuntimeService,
    SkillCodexUnavailable,
)

router = APIRouter(prefix="/functions", tags=["functions"])
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@router.get("/catalog", response_model=list[FunctionCatalogEntryRead])
def list_function_catalog(db: Session = Depends(get_db)) -> list[dict]:
    return FunctionCatalogService(db).list_entries(include_runtime_state=True)


@router.get("", response_model=list[FunctionContractRead])
def list_functions(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> list[FunctionContractRead]:
    service = FunctionRegistryService(db, project_root=PROJECT_ROOT)
    if authorization is None:
        return service.list_contracts()
    try:
        caller = service.caller_from_capability(_bearer_token(authorization))
    except FunctionRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return service.list_contracts(caller.skill)


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
        caller = FunctionRegistryService(
            db,
            project_root=PROJECT_ROOT,
        ).caller_from_capability(_bearer_token(authorization))
        result = SkillCodexRuntimeService(db, project_root=PROJECT_ROOT).call(
            caller.skill,
            payload,
            expected_version_id=caller.version_id,
        )
    except FunctionRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except SkillCodexInvalidRequest as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SkillCodexUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CodexGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return SkillCodexResponse(**result)


@router.post("/{function_name}/invoke", response_model=FunctionInvocationResponse)
def invoke_function(
    function_name: str,
    payload: FunctionInvocationRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FunctionInvocationResponse:
    try:
        run = FunctionRegistryService(db, project_root=PROJECT_ROOT).invoke_from_capability(
            _bearer_token(authorization),
            function_name,
            payload.input,
        )
    except FunctionRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(run, InvocationApproval):
        receipt = {"status": "pending_approval", "approval_id": run.id}
        return FunctionInvocationResponse(output=receipt, approval=receipt)
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
