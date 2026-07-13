from typing import Any

from fastapi import APIRouter, Request

from app.services.codex_cli_service import codex_cli_service
from app.services.codex_usage_service import codex_usage_service

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/codex")
def get_codex_usage(request: Request) -> dict[str, Any]:
    service = getattr(request.app.state, "codex_usage_service", codex_usage_service)
    return service.read_account_usage()


@router.get("/codex/cli")
def get_codex_cli_status(refresh: bool = False) -> dict[str, object]:
    return codex_cli_service.read_status(refresh=refresh)
