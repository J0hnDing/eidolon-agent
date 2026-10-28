from dataclasses import asdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.models import CodexMcpSettings
from app.schemas.integration import IntegrationInvocationRequest
from app.schemas.whatsapp import WhatsAppHistoryRequest, WhatsAppMusePermissionUpdate, WhatsAppStatus
from app.services.agent_policy_service import AgentPermissionError
from app.services.function_catalog_service import FunctionCatalogService
from app.services.whatsapp_bridge import WhatsAppError
from app.services.whatsapp_service import WhatsAppService

router = APIRouter(prefix="/settings/integrations/whatsapp", tags=["integrations"])
invocation_router = APIRouter(prefix="/integrations/whatsapp/mcp", tags=["integrations"])


@invocation_router.post("/invoke", include_in_schema=False)
def whatsapp_mcp_invoke(
    payload: IntegrationInvocationRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    # This is native local MCP transport, not a web-app or browser gateway.
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"} or request.headers.get("origin"):
        raise HTTPException(status_code=403, detail="Local native MCP transport is required")
    operation = DEFAULT_INTEGRATION_REGISTRY.get(payload.operation)
    if operation is None or not DEFAULT_INTEGRATION_REGISTRY.supports_provider(payload.operation, "whatsapp"):
        raise HTTPException(status_code=404, detail="WhatsApp operation is unavailable")
    try:
        factory = InvocationContextFactory(db)
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise AgentPermissionError("An agent credential is required")
            # Invalid/revoked credentials never fall back to direct-user authority.
            context = factory.from_agent_credential(token.strip())
        else:
            settings = db.get(CodexMcpSettings, 1)
            if settings is None or not settings.enabled:
                return asdict(InvocationOutcome(status="failed", error_type="mcp_disabled",
                                               error_message="Eidolon Codex tools are disabled in Settings"))
            context = factory.direct_user(origin="codex_mcp", initiating_action="codex_mcp")
        outcome = InvocationExecutor(db).execute(
            InvocationTargetRef(category="integration", target_id=payload.operation), payload.input, context,
        )
        return asdict(outcome)
    except AgentPermissionError:
        return asdict(InvocationOutcome(status="failed", error_type="agent_permission_denied",
                                       error_message="The agent credential is invalid or no longer active"))
    except InvocationExecutionError as exc:
        return asdict(InvocationOutcome(status="failed", error_type=exc.error_type, error_message=str(exc)))


@router.get("", response_model=WhatsAppStatus)
def whatsapp_status(db: Session = Depends(get_db)) -> WhatsAppStatus:
    return WhatsAppService(db).status()


@router.get("/pairing", response_model=WhatsAppStatus)
def whatsapp_pairing(db: Session = Depends(get_db)) -> WhatsAppStatus:
    result = WhatsAppService(db).status(pairing=True)
    if result.connected:
        FunctionCatalogService(db).refresh()
    return result


@router.get("/diagnostics")
def whatsapp_diagnostics(db: Session = Depends(get_db)) -> dict:
    try:
        return WhatsAppService(db).diagnostics()
    except WhatsAppError as exc:
        raise HTTPException(status_code=409, detail={"error_type": exc.error_type,
                                                    "message": "WhatsApp diagnostics are unavailable"}) from None


@router.post("/agents/discover")
def whatsapp_discover_agents(db: Session = Depends(get_db)) -> dict:
    try:
        return WhatsAppService(db).diagnostics(discover_agents=True)
    except WhatsAppError as exc:
        raise HTTPException(status_code=409, detail={"error_type": exc.error_type,
                                                    "message": "WhatsApp agent directory is unavailable"}) from None


@router.post("/history")
def whatsapp_request_history(payload: WhatsAppHistoryRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return WhatsAppService(db).request_history(payload.chat_id)
    except WhatsAppError as exc:
        raise HTTPException(status_code=409, detail={"error_type": exc.error_type,
                                                    "message": "WhatsApp history request could not be completed"}) from None


@router.put("/muse", response_model=WhatsAppStatus)
def whatsapp_muse_permission(payload: WhatsAppMusePermissionUpdate, db: Session = Depends(get_db)) -> WhatsAppStatus:
    try:
        result = WhatsAppService(db).configure_muse(enabled=payload.enabled, account_id=payload.account_id)
    except WhatsAppError as exc:
        raise HTTPException(status_code=409, detail={"error_type": exc.error_type,
                                                    "message": "Muse permission could not be changed. Check the linked account and Assistant policy."}) from None
    FunctionCatalogService(db).refresh()
    return result


def _change(db: Session, action: str) -> WhatsAppStatus:
    try:
        result = getattr(WhatsAppService(db), action)()
    except WhatsAppError as exc:
        message = {
            "dependencies_missing": "Build the Whatsmeow bridge with bridges/whatsapp/build.ps1 before pairing.",
            "secret_store_unavailable": "Windows Credential Manager is unavailable; WhatsApp credentials cannot be accessed.",
            "cleanup_pending": "Local WhatsApp cleanup is incomplete. Retry Disconnect before pairing again.",
            "pairing_required": "Pair WhatsApp before reconnecting.",
        }.get(exc.error_type, "WhatsApp operation could not be completed")
        raise HTTPException(status_code=409, detail={"error_type": exc.error_type,
                                                    "message": message}) from None
    FunctionCatalogService(db).refresh()
    return result


@router.post("/pairing/start", response_model=WhatsAppStatus)
def whatsapp_pair(db: Session = Depends(get_db)) -> WhatsAppStatus:
    return _change(db, "pair")


@router.post("/reconnect", response_model=WhatsAppStatus)
def whatsapp_reconnect(db: Session = Depends(get_db)) -> WhatsAppStatus:
    return _change(db, "reconnect")


@router.delete("/cache", response_model=WhatsAppStatus)
def whatsapp_clear(db: Session = Depends(get_db)) -> WhatsAppStatus:
    return _change(db, "clear_cache")


@router.delete("", response_model=WhatsAppStatus)
def whatsapp_disconnect(db: Session = Depends(get_db)) -> WhatsAppStatus:
    return _change(db, "disconnect")
