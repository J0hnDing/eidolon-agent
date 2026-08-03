from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.codex_routing import (
    CodexModelCatalogRead,
    CodexRoutingSettingsPayload,
    CodexRoutingSettingsRead,
)
from app.schemas.permission_policy import PermissionPolicyRead
from app.services.codex_routing_service import CodexRoutingError, CodexRoutingService
from app.services.default_permissions import default_permission_policy

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/permission-policy", response_model=PermissionPolicyRead)
def get_permission_policy() -> dict[str, object]:
    return {
        "source": "backend/app/static/default_permissions.json",
        **default_permission_policy(),
    }


@router.get("/codex-routing", response_model=CodexRoutingSettingsRead)
def get_codex_routing_settings(db: Session = Depends(get_db)) -> CodexRoutingSettingsRead:
    return CodexRoutingService(db).read_settings()


@router.put("/codex-routing", response_model=CodexRoutingSettingsRead)
def update_codex_routing_settings(
    payload: CodexRoutingSettingsPayload,
    db: Session = Depends(get_db),
) -> CodexRoutingSettingsRead:
    try:
        return CodexRoutingService(db).update_settings(payload)
    except CodexRoutingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/codex-models", response_model=CodexModelCatalogRead)
def get_codex_models(refresh: bool = False, db: Session = Depends(get_db)) -> dict[str, object]:
    return CodexRoutingService(db).read_model_catalog(refresh=refresh)
