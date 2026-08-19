from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.atlas_settings import (
    AtlasDirectoryWrite,
    AtlasPassphraseWrite,
    AtlasSettingsStatus,
)
from app.services.atlas_settings_service import (
    AtlasSettingsError,
    build_default_atlas_settings_service,
)

router = APIRouter(prefix="/settings/integrations/atlas", tags=["integrations"])


@router.get("", response_model=AtlasSettingsStatus)
def atlas_status(db: Session = Depends(get_db)) -> AtlasSettingsStatus:
    return build_default_atlas_settings_service(db).status()


@router.put("/directory", response_model=AtlasSettingsStatus)
def save_atlas_directory(payload: AtlasDirectoryWrite, db: Session = Depends(get_db)) -> AtlasSettingsStatus:
    try:
        return build_default_atlas_settings_service(db).save_directory_and_restart(Path(payload.directory))
    except AtlasSettingsError as exc:
        raise _http_error(exc) from None


@router.post("/restart", response_model=AtlasSettingsStatus)
def restart_atlas(db: Session = Depends(get_db)) -> AtlasSettingsStatus:
    service = build_default_atlas_settings_service(db)
    return service.restart()


@router.put("/passphrase", response_model=AtlasSettingsStatus)
def put_atlas_passphrase(payload: AtlasPassphraseWrite, db: Session = Depends(get_db)) -> AtlasSettingsStatus:
    try:
        return build_default_atlas_settings_service(db).put_passphrase(payload.passphrase.get_secret_value())
    except AtlasSettingsError as exc:
        raise _http_error(exc) from None


@router.delete("/passphrase", status_code=status.HTTP_204_NO_CONTENT)
def remove_atlas_passphrase(db: Session = Depends(get_db)) -> Response:
    try:
        build_default_atlas_settings_service(db).remove_passphrase()
    except AtlasSettingsError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/unlock", response_model=AtlasSettingsStatus)
def unlock_atlas(db: Session = Depends(get_db)) -> AtlasSettingsStatus:
    try:
        return build_default_atlas_settings_service(db).unlock_now()
    except AtlasSettingsError as exc:
        raise _http_error(exc) from None


def _http_error(exc: AtlasSettingsError) -> HTTPException:
    status_code = {
        "invalid_input": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "invalid_directory": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "invalid_passphrase": status.HTTP_401_UNAUTHORIZED,
        "passphrase_missing": status.HTTP_409_CONFLICT,
        "atlas_uninitialized": status.HTTP_409_CONFLICT,
        "external_unlock_forbidden": status.HTTP_409_CONFLICT,
        "legacy_credentials_pending_cleanup": status.HTTP_409_CONFLICT,
    }.get(exc.error_type, status.HTTP_503_SERVICE_UNAVAILABLE)
    return HTTPException(status_code=status_code, detail={"type": exc.error_type, "message": str(exc)})
