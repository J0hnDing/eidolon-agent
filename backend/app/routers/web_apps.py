import asyncio
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.request import Request as UrlRequest

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.models import Skill, SkillRun, WebAppAuditRecord, WebAppInstance
from app.schemas.function_registry import FunctionInvocationRequest, FunctionInvocationResponse
from app.schemas.integration import IntegrationInvocationRequest, IntegrationInvocationResponse
from app.schemas.manifest import SkillManifest
from app.schemas.skill_codex import SkillCodexRequest, SkillCodexResponse
from app.schemas.skill_run import SkillRunRead
from app.schemas.web_app import WebAppAuditRecordRead, WebAppInstanceRead, WebAppOpenResponse
from app.services.function_registry_service import FunctionRegistryError
from app.services.manifest_validator import validate_manifest_file
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.web_app_runtime_service import WebAppRuntimeError, WebAppRuntimeService

router = APIRouter(prefix="/web-apps", tags=["web-apps"])
gateway_router = APIRouter(tags=["web-app-gateway"])


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


@router.post("/{skill_id}/sessions", response_model=WebAppOpenResponse)
def open_web_app(skill_id: int, db: Session = Depends(get_db)) -> WebAppOpenResponse:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        return WebAppRuntimeService(db).open_session(skill)
    except WebAppRuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{skill_id}/instances", response_model=list[WebAppInstanceRead])
def list_web_app_instances(skill_id: int, db: Session = Depends(get_db)) -> list[WebAppInstance]:
    if db.get(Skill, skill_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return list(
        db.scalars(
            select(WebAppInstance)
            .where(WebAppInstance.skill_id == skill_id)
            .order_by(WebAppInstance.created_at.desc())
        ).all()
    )


@router.get("/{skill_id}/audit", response_model=list[WebAppAuditRecordRead])
def list_web_app_audit(skill_id: int, db: Session = Depends(get_db)) -> list[WebAppAuditRecord]:
    if db.get(Skill, skill_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    return list(
        db.scalars(
            select(WebAppAuditRecord)
            .join(WebAppInstance, WebAppAuditRecord.instance_id == WebAppInstance.id)
            .where(WebAppInstance.skill_id == skill_id)
            .order_by(WebAppAuditRecord.id.desc())
            .limit(200)
        ).all()
    )


@router.post("/{skill_id}/stop", response_model=list[WebAppInstanceRead])
def stop_web_app(skill_id: int, db: Session = Depends(get_db)) -> list[WebAppInstance]:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    try:
        with SkillOperationGuard(db).locked(skill, "web_app_stop", reason="Stopping web application"):
            WebAppRuntimeService(db).stop_skill_instances(skill, "Stopped by local user")
    except SkillOperationConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return list(
        db.scalars(
            select(WebAppInstance)
            .where(WebAppInstance.skill_id == skill_id)
            .order_by(WebAppInstance.created_at.desc())
        ).all()
    )


@router.post("/capabilities/codex", response_model=SkillCodexResponse)
def web_app_codex_capability(
    payload: SkillCodexRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> SkillCodexResponse:
    token = _bearer_token(authorization)
    runtime = WebAppRuntimeService(db)
    try:
        context = InvocationContextFactory(
            db,
            project_root=runtime.project_root,
            web_app_runtime=runtime,
        ).from_web_app_capability(
            token,
            initiating_action="backend.codex.call",
        )
        instance = db.get(WebAppInstance, context.web_app_instance_id)
        outcome = InvocationExecutor(db, project_root=runtime.project_root).execute(
            InvocationTargetRef(category="backend_core", target_id="backend.codex.call"),
            payload.model_dump(mode="json"),
            context,
        )
    except WebAppRuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except InvocationExecutionError as exc:
        instance = db.get(WebAppInstance, context.web_app_instance_id) if "context" in locals() else None
        if instance is not None:
            runtime.record_audit(
                instance,
                "codex_call",
                "failed",
                request={
                    "prompt_characters": len(payload.prompt),
                    "internet_access": payload.codex_permissions.internet_access,
                },
                error_message=str(exc),
            )
        code = status.HTTP_502_BAD_GATEWAY if exc.error_type == "codex_failed" else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if instance is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Web application is unavailable")
    internet_requested = payload.codex_permissions.internet_access
    if outcome.output is None:
        runtime.record_audit(
            instance,
            "codex_call",
            "failed",
            request={"prompt_characters": len(payload.prompt), "internet_access": internet_requested},
            error_message="Codex invocation returned no output",
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Codex invocation returned no output")
    runtime.record_audit(
        instance,
        "codex_call",
        "succeeded",
        request={"prompt_characters": len(payload.prompt), "internet_access": internet_requested},
        response={"response_characters": len(str(outcome.output.get("response", "")))},
    )
    return SkillCodexResponse(**outcome.output)


@router.post("/capabilities/functions/{function_name}", response_model=FunctionInvocationResponse)
def web_app_function_capability(
    function_name: str,
    payload: FunctionInvocationRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FunctionInvocationResponse:
    token = _bearer_token(authorization)
    runtime = WebAppRuntimeService(db)
    try:
        context = InvocationContextFactory(
            db,
            project_root=runtime.project_root,
            web_app_runtime=runtime,
        ).from_web_app_capability(
            token,
            initiating_action=None,
        )
        instance = db.get(WebAppInstance, context.web_app_instance_id)
        outcome = InvocationExecutor(db, project_root=runtime.project_root).execute(
            InvocationTargetRef(category="user", target_id=function_name),
            payload.input,
            context,
        )
    except (FunctionRegistryError, WebAppRuntimeError, InvocationExecutionError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if instance is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Web application is unavailable")
    if outcome.approval_id is not None:
        receipt = {"status": "pending_approval", "approval_id": outcome.approval_id}
        runtime.record_audit(
            instance,
            "function_call",
            "pending_approval",
            request={"function_name": function_name, "input": payload.input},
            response={"approval_id": outcome.approval_id, "status": "pending_approval"},
        )
        return FunctionInvocationResponse(output=receipt, approval=receipt)
    run = db.get(SkillRun, outcome.skill_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Function run was not recorded")
    runtime.record_audit(
        instance,
        "function_call",
        run.status,
        request={"function_name": function_name, "input": payload.input},
        response={"run_id": run.id, "status": run.status},
        error_message=run.error_message,
    )
    return FunctionInvocationResponse(
        run=SkillRunRead.model_validate(run),
        output=run.output_json,
        error=run.error_message,
    )


@router.post(
    "/capabilities/integrations/invoke",
    response_model=IntegrationInvocationResponse,
    include_in_schema=False,
)
def web_app_integration_capability(
    payload: IntegrationInvocationRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> IntegrationInvocationResponse:
    token = _bearer_token(authorization)
    runtime = WebAppRuntimeService(db)
    try:
        context = InvocationContextFactory(
            db,
            project_root=runtime.project_root,
            web_app_runtime=runtime,
        ).from_web_app_capability(
            token,
            initiating_action=f"integration:{payload.operation}",
        )
        outcome = InvocationExecutor(db, project_root=runtime.project_root).execute(
            InvocationTargetRef(category="integration", target_id=payload.operation),
            payload.input,
            context,
        )
    except WebAppRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"type": "authorization_missing_or_stale", "message": str(exc)},
        ) from None
    except InvocationExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"type": exc.error_type, "message": str(exc)},
        ) from None
    return IntegrationInvocationResponse(output={} if outcome.output is None else outcome.output)


@gateway_router.api_route(
    "/__web_app_gateway/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_web_app(path: str, request: Request, db: Session = Depends(get_db)) -> Response:
    runtime = WebAppRuntimeService(db)
    try:
        session, instance, _skill = runtime.resolve_gateway_session(request.headers.get("host", ""))
    except WebAppRuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if request.headers.get("upgrade", "").lower() == "websocket":
        raise HTTPException(status_code=status.HTTP_426_UPGRADE_REQUIRED, detail="WebSockets are not supported")
    if any(part == ".." for part in path.replace("\\", "/").split("/")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid application path")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > runtime.config.max_request_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Application request is too large",
            )
    query = f"?{request.url.query}" if request.url.query else ""
    upstream_url = f"{instance.upstream_url}/{path}{query}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in {"accept", "accept-language", "content-type", "if-none-match", "if-modified-since"}
    }
    headers["X-Personal-Agent-Session"] = session.id
    upstream_request = UrlRequest(
        upstream_url,
        data=bytes(body) if body else None,
        headers=headers,
        method=request.method,
    )
    try:
        response_body, response_status, content_type = await asyncio.to_thread(
            _read_upstream,
            upstream_request,
            runtime.config.max_response_bytes,
        )
    except (OSError, URLError, WebAppRuntimeError) as exc:
        runtime.mark_unhealthy(instance, f"Gateway could not reach the application: {exc}")
        runtime.record_audit(
            instance,
            "gateway_request",
            "failed",
            session=session,
            request={"method": request.method, "path": f"/{path}"[:512]},
            error_message=str(exc),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    runtime.record_audit(
        instance,
        "gateway_request",
        "succeeded",
        session=session,
        request={"method": request.method, "path": f"/{path}"[:512]},
        response={"status": response_status, "bytes": len(response_body)},
    )
    policy = runtime.containment_policy(instance.runner_mode, _manifest(db, instance))
    security_headers = {
        "Content-Type": content_type,
        "Content-Security-Policy": policy.content_security_policy,
        "Permissions-Policy": policy.permissions_policy,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cache-Control": "no-store",
    }
    return Response(
        content=b"" if request.method == "HEAD" else response_body,
        status_code=response_status,
        headers=security_headers,
    )


def _manifest(db: Session, instance: WebAppInstance) -> SkillManifest:
    skill = db.get(Skill, instance.skill_id)
    if skill is None:
        raise WebAppRuntimeError("Web application skill no longer exists")
    service = WebAppRuntimeService(db)
    return validate_manifest_file(service.proposed_service.skill_dir_for_record(skill) / "manifest.json")


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer capability token is required")
    return token.strip()


def _read_upstream(upstream_request: UrlRequest, max_response_bytes: int) -> tuple[bytes, int, str]:
    try:
        upstream = build_opener(_NoRedirect()).open(upstream_request, timeout=10)
    except HTTPError as exc:
        upstream = exc
    try:
        response_body = upstream.read(max_response_bytes + 1)
        if len(response_body) > max_response_bytes:
            raise WebAppRuntimeError("Application response exceeded the gateway size limit")
        response_status = int(upstream.status)
        if 300 <= response_status < 400:
            raise WebAppRuntimeError("Application redirects are not supported by the controlled gateway")
        content_type = upstream.headers.get("Content-Type", "application/octet-stream")
        return response_body, response_status, content_type
    finally:
        close = getattr(upstream, "close", None)
        if callable(close):
            close()
