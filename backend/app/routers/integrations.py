from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.integration import (
    GitHubConnectionStatus,
    GitHubCredentialWrite,
    GmailConnectionStatus,
    GoogleCalendarConnectionStatus,
    GoogleOAuthClientStatus,
    GoogleOAuthClientWrite,
    GoogleOAuthStartResponse,
    IntegrationInvocationRequest,
    IntegrationInvocationResponse,
    NotionConnectionStatus,
    NotionCredentialWrite,
    NotionDataSourcesWrite,
    QuercusConnectionStatus,
    QuercusCourseRead,
    QuercusCourseSelectionWrite,
    QuercusCredentialWrite,
    QuercusProcessingReprocessResult,
    QuercusProcessingStatus,
    QuercusProcessingWrite,
    TelegramConnectionStatus,
    TelegramPairingResponse,
    TelegramPairingStart,
)
from app.services.function_catalog_service import FunctionCatalogService
from app.services.function_registry_service import FunctionRegistryError, FunctionRegistryService
from app.services.gmail_provider import GMAIL_OAUTH_RETURN_URL
from app.services.google_calendar_provider import GOOGLE_OAUTH_RETURN_URL
from app.services.integration_service import (
    IntegrationCaller,
    IntegrationError,
    build_default_integration_service,
)
from app.services.quercus_processing_service import (
    PROCESSING_MARKER,
    QuercusProcessingService,
)
from app.services.quercus_service import QuercusError, QuercusService
from app.services.telegram_service import TELEGRAM_ACT_ROLE, TelegramService, TelegramServiceError

router = APIRouter(tags=["integrations"])


def _quercus_service(request: Request, db: Session) -> QuercusService:
    dispatcher = getattr(request.app.state, "quercus_sync_dispatcher", None)
    return QuercusService(db, queue_sync=getattr(dispatcher, "request", None))


def _quercus_processing_service(db: Session) -> QuercusProcessingService:
    return QuercusProcessingService(db)


@router.get("/settings/integrations/github", response_model=GitHubConnectionStatus)
def github_connection_status(db: Session = Depends(get_db)) -> GitHubConnectionStatus:
    return build_default_integration_service(db).connection_status()


@router.put("/settings/integrations/github", response_model=GitHubConnectionStatus)
def put_github_connection(
    payload: GitHubCredentialWrite,
    db: Session = Depends(get_db),
) -> GitHubConnectionStatus:
    try:
        result = build_default_integration_service(db).put_github_connection(payload.token.get_secret_value())
        FunctionCatalogService(db).refresh()
        return result
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.delete("/settings/integrations/github", status_code=status.HTTP_204_NO_CONTENT)
def remove_github_connection(db: Session = Depends(get_db)) -> Response:
    try:
        build_default_integration_service(db).remove_github_connection()
        FunctionCatalogService(db).refresh()
    except IntegrationError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings/integrations/notion", response_model=NotionConnectionStatus)
def notion_connection_status(db: Session = Depends(get_db)) -> NotionConnectionStatus:
    return build_default_integration_service(db).notion_connection_status()


@router.put("/settings/integrations/notion", response_model=NotionConnectionStatus)
def put_notion_connection(
    payload: NotionCredentialWrite,
    db: Session = Depends(get_db),
) -> NotionConnectionStatus:
    try:
        result = build_default_integration_service(db).put_notion_credential(
            payload.token.get_secret_value(),
        )
        FunctionCatalogService(db).refresh()
        return result
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.put(
    "/settings/integrations/notion/data-sources",
    response_model=NotionConnectionStatus,
)
def put_notion_data_sources(
    payload: NotionDataSourcesWrite,
    db: Session = Depends(get_db),
) -> NotionConnectionStatus:
    try:
        result = build_default_integration_service(db).put_notion_data_sources(
            payload.data_source_id,
            payload.report_data_source_id,
        )
        FunctionCatalogService(db).refresh()
        return result
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.delete(
    "/settings/integrations/notion/data-sources",
    response_model=NotionConnectionStatus,
)
def remove_notion_data_sources(db: Session = Depends(get_db)) -> NotionConnectionStatus:
    try:
        result = build_default_integration_service(db).remove_notion_data_sources()
        FunctionCatalogService(db).refresh()
        return result
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.delete("/settings/integrations/notion", status_code=status.HTTP_204_NO_CONTENT)
def remove_notion_connection(db: Session = Depends(get_db)) -> Response:
    try:
        build_default_integration_service(db).remove_notion_connection()
        FunctionCatalogService(db).refresh()
    except IntegrationError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings/integrations/quercus", response_model=QuercusConnectionStatus)
def quercus_connection_status(request: Request, db: Session = Depends(get_db)) -> QuercusConnectionStatus:
    return _quercus_service(request, db).status()


@router.put("/settings/integrations/quercus", response_model=QuercusConnectionStatus)
def put_quercus_connection(
    payload: QuercusCredentialWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> QuercusConnectionStatus:
    try:
        return _quercus_service(request, db).put_connection(payload.token.get_secret_value())
    except QuercusError as exc:
        raise _http_error(exc) from None


@router.delete("/settings/integrations/quercus", response_model=QuercusConnectionStatus)
def remove_quercus_connection(request: Request, db: Session = Depends(get_db)) -> QuercusConnectionStatus:
    try:
        return _quercus_service(request, db).remove_connection()
    except QuercusError as exc:
        raise _http_error(exc) from None


@router.get(
    "/settings/integrations/quercus/processing",
    response_model=QuercusProcessingStatus,
)
def quercus_processing_status(db: Session = Depends(get_db)) -> QuercusProcessingStatus:
    return QuercusProcessingStatus(**_quercus_processing_service(db).status())


@router.put(
    "/settings/integrations/quercus/processing",
    response_model=QuercusProcessingStatus,
)
def put_quercus_processing(
    payload: QuercusProcessingWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> QuercusProcessingStatus:
    service = _quercus_processing_service(db)
    try:
        service.configure(payload.method, payload.llama_cpp_directory)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"type": "invalid_directory", "message": str(exc)},
        ) from None
    if payload.method == PROCESSING_MARKER:
        dispatcher = getattr(request.app.state, "quercus_processing_dispatcher", None)
        if dispatcher is not None:
            dispatcher.request()
    return QuercusProcessingStatus(**service.status())


@router.post(
    "/settings/integrations/quercus/processing/reprocess-failed",
    response_model=QuercusProcessingReprocessResult,
)
def reprocess_failed_quercus_files(
    request: Request,
    db: Session = Depends(get_db),
) -> QuercusProcessingReprocessResult:
    service = _quercus_processing_service(db)
    try:
        queued_file_count = service.requeue_failed()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"type": "processing_disabled", "message": str(exc)},
        ) from None
    dispatcher = getattr(request.app.state, "quercus_processing_dispatcher", None)
    if queued_file_count and dispatcher is not None:
        dispatcher.request()
    return QuercusProcessingReprocessResult(
        **service.status(),
        queued_file_count=queued_file_count,
    )


@router.get("/settings/integrations/quercus/courses", response_model=list[QuercusCourseRead])
def list_quercus_courses(request: Request, db: Session = Depends(get_db)) -> list[QuercusCourseRead]:
    try:
        return _quercus_service(request, db).courses()
    except QuercusError as exc:
        raise _http_error(exc) from None


@router.put("/settings/integrations/quercus/courses", response_model=list[QuercusCourseRead])
def select_quercus_courses(
    payload: QuercusCourseSelectionWrite,
    request: Request,
    db: Session = Depends(get_db),
) -> list[QuercusCourseRead]:
    try:
        return _quercus_service(request, db).select_courses(payload.course_ids)
    except QuercusError as exc:
        raise _http_error(exc) from None


@router.delete(
    "/settings/integrations/quercus/courses/{course_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_quercus_course(
    course_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    try:
        _quercus_service(request, db).delete_course(course_id)
    except QuercusError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/settings/integrations/google",
    response_model=GoogleOAuthClientStatus,
)
def google_oauth_client_status(db: Session = Depends(get_db)) -> GoogleOAuthClientStatus:
    return build_default_integration_service(db).google_oauth_client_status()


@router.put(
    "/settings/integrations/google/oauth-client",
    response_model=GoogleOAuthClientStatus,
)
def put_google_oauth_client(
    payload: GoogleOAuthClientWrite,
    db: Session = Depends(get_db),
) -> GoogleOAuthClientStatus:
    try:
        return build_default_integration_service(db).configure_google_oauth_client(
            payload.client_id.get_secret_value(),
            payload.client_secret.get_secret_value(),
        )
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.delete(
    "/settings/integrations/google/oauth-client",
    response_model=GoogleOAuthClientStatus,
)
def remove_google_oauth_client(db: Session = Depends(get_db)) -> GoogleOAuthClientStatus:
    try:
        return build_default_integration_service(db).remove_google_oauth_client()
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.get(
    "/settings/integrations/google-calendar",
    response_model=GoogleCalendarConnectionStatus,
)
def google_calendar_connection_status(db: Session = Depends(get_db)) -> GoogleCalendarConnectionStatus:
    return build_default_integration_service(db).google_calendar_connection_status()


@router.post(
    "/settings/integrations/google-calendar/oauth/start",
    response_model=GoogleOAuthStartResponse,
)
def start_google_calendar_oauth(
    db: Session = Depends(get_db),
) -> GoogleOAuthStartResponse:
    try:
        authorization_url = build_default_integration_service(db).start_google_calendar_oauth()
        return GoogleOAuthStartResponse(authorization_url=authorization_url)
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.get(
    "/settings/integrations/google-calendar/oauth/callback",
    response_class=RedirectResponse,
    include_in_schema=False,
)
def complete_google_calendar_oauth(
    state_value: str = Query(default="", alias="state"),
    code_value: str = Query(default="", alias="code"),
    error: str = Query(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = build_default_integration_service(db)
    if error:
        try:
            service.discard_google_calendar_oauth(state_value)
        except IntegrationError:
            result = "failed"
        else:
            result = "denied" if error == "access_denied" else "failed"
        return RedirectResponse(f"{GOOGLE_OAUTH_RETURN_URL}?google_calendar={result}", status_code=303)
    try:
        service.complete_google_calendar_oauth(state_value, code_value)
        FunctionCatalogService(db).refresh()
    except IntegrationError:
        return RedirectResponse(f"{GOOGLE_OAUTH_RETURN_URL}?google_calendar=failed", status_code=303)
    finally:
        code_value = ""
    return RedirectResponse(f"{GOOGLE_OAUTH_RETURN_URL}?google_calendar=connected", status_code=303)


@router.delete(
    "/settings/integrations/google-calendar",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_google_calendar_connection(db: Session = Depends(get_db)) -> Response:
    try:
        build_default_integration_service(db).remove_google_calendar_connection()
        FunctionCatalogService(db).refresh()
    except IntegrationError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings/integrations/gmail", response_model=GmailConnectionStatus)
def gmail_connection_status(db: Session = Depends(get_db)) -> GmailConnectionStatus:
    return build_default_integration_service(db).gmail_connection_status()


@router.post("/settings/integrations/gmail/oauth/start", response_model=GoogleOAuthStartResponse)
def start_gmail_oauth(db: Session = Depends(get_db)) -> GoogleOAuthStartResponse:
    try:
        authorization_url = build_default_integration_service(db).start_gmail_oauth()
        return GoogleOAuthStartResponse(authorization_url=authorization_url)
    except IntegrationError as exc:
        raise _http_error(exc) from None


@router.get(
    "/settings/integrations/gmail/oauth/callback",
    response_class=RedirectResponse,
    include_in_schema=False,
)
def complete_gmail_oauth(
    state_value: str = Query(default="", alias="state"),
    code_value: str = Query(default="", alias="code"),
    error: str = Query(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    service = build_default_integration_service(db)
    if error:
        try:
            service.discard_gmail_oauth(state_value)
        except IntegrationError:
            result = "failed"
        else:
            result = "denied" if error == "access_denied" else "failed"
        return RedirectResponse(f"{GMAIL_OAUTH_RETURN_URL}?gmail={result}", status_code=303)
    try:
        service.complete_gmail_oauth(state_value, code_value)
        FunctionCatalogService(db).refresh()
    except IntegrationError:
        return RedirectResponse(f"{GMAIL_OAUTH_RETURN_URL}?gmail=failed", status_code=303)
    finally:
        code_value = ""
    return RedirectResponse(f"{GMAIL_OAUTH_RETURN_URL}?gmail=connected", status_code=303)


@router.delete("/settings/integrations/gmail", status_code=status.HTTP_204_NO_CONTENT)
def remove_gmail_connection(db: Session = Depends(get_db)) -> Response:
    try:
        build_default_integration_service(db).remove_gmail_connection()
        FunctionCatalogService(db).refresh()
    except IntegrationError as exc:
        raise _http_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings/integrations/telegram", response_model=TelegramConnectionStatus)
def telegram_connection_status(db: Session = Depends(get_db)) -> TelegramConnectionStatus:
    return TelegramService(db).connection_status()


@router.post("/settings/integrations/telegram/pairing/start", response_model=TelegramPairingResponse)
def start_telegram_pairing(
    payload: TelegramPairingStart,
    db: Session = Depends(get_db),
) -> TelegramPairingResponse:
    try:
        result = TelegramService(db).start_pairing(payload.token.get_secret_value())
        FunctionCatalogService(db).refresh()
        return result
    except TelegramServiceError as exc:
        raise _http_error(IntegrationError(exc.error_type, str(exc))) from None


@router.post("/settings/integrations/telegram/pairing/refresh", response_model=TelegramConnectionStatus)
def refresh_telegram_pairing(db: Session = Depends(get_db)) -> TelegramConnectionStatus:
    return TelegramService(db).connection_status()


@router.delete("/settings/integrations/telegram", status_code=status.HTTP_204_NO_CONTENT)
def remove_telegram_connection(db: Session = Depends(get_db)) -> Response:
    try:
        TelegramService(db).remove()
        FunctionCatalogService(db).refresh()
    except TelegramServiceError as exc:
        raise _http_error(IntegrationError(exc.error_type, str(exc))) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings/integrations/telegram-agent", response_model=TelegramConnectionStatus)
def telegram_agent_connection_status(db: Session = Depends(get_db)) -> TelegramConnectionStatus:
    return TelegramService(db, role=TELEGRAM_ACT_ROLE).connection_status()


@router.post("/settings/integrations/telegram-agent/pairing/start", response_model=TelegramPairingResponse)
def start_telegram_agent_pairing(payload: TelegramPairingStart, db: Session = Depends(get_db)) -> TelegramPairingResponse:
    try:
        return TelegramService(db, role=TELEGRAM_ACT_ROLE).start_pairing(payload.token.get_secret_value())
    except TelegramServiceError as exc:
        raise _http_error(IntegrationError(exc.error_type, str(exc))) from None


@router.post("/settings/integrations/telegram-agent/pairing/refresh", response_model=TelegramConnectionStatus)
def refresh_telegram_agent_pairing(db: Session = Depends(get_db)) -> TelegramConnectionStatus:
    return TelegramService(db, role=TELEGRAM_ACT_ROLE).connection_status()


@router.delete("/settings/integrations/telegram-agent", status_code=status.HTTP_204_NO_CONTENT)
def remove_telegram_agent_connection(db: Session = Depends(get_db)) -> Response:
    try:
        TelegramService(db, role=TELEGRAM_ACT_ROLE).remove()
    except TelegramServiceError as exc:
        raise _http_error(IntegrationError(exc.error_type, str(exc))) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/integrations/capabilities/invoke",
    response_model=IntegrationInvocationResponse,
    include_in_schema=False,
)
def invoke_function_integration(
    payload: IntegrationInvocationRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> IntegrationInvocationResponse:
    try:
        caller = FunctionRegistryService(db).caller_from_capability(_bearer_token(authorization))
        output = build_default_integration_service(db).invoke(
            IntegrationCaller(
                skill_id=caller.skill.id,
                version_id=caller.version_id,
                runtime=caller.skill.runtime,
                skill_run_id=caller.run_id,
            ),
            payload.operation,
            payload.input,
        )
    except FunctionRegistryError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"type": "authorization_missing_or_stale", "message": str(exc)},
        ) from None
    except IntegrationError as exc:
        raise _http_error(exc) from None
    return IntegrationInvocationResponse(output=output)


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"type": "authorization_missing_or_stale", "message": "Runtime capability is required"},
        )
    return token.strip()


def _http_error(exc: IntegrationError | QuercusError) -> HTTPException:
    status_code = {
        "invalid_input": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "invalid_credential": status.HTTP_401_UNAUTHORIZED,
        "provider_forbidden": status.HTTP_403_FORBIDDEN,
        "not_found": status.HTTP_404_NOT_FOUND,
        "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
        "provider_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
        "response_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
        "provider_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
        "identity_conflict": status.HTTP_409_CONFLICT,
        "connection_unavailable": status.HTTP_409_CONFLICT,
    }.get(exc.error_type, status.HTTP_409_CONFLICT)
    retry_after_seconds = getattr(exc, "retry_after_seconds", None)
    headers = (
        {"Retry-After": str(retry_after_seconds)}
        if exc.error_type == "rate_limited" and retry_after_seconds is not None
        else None
    )
    return HTTPException(
        status_code=status_code,
        detail={"type": exc.error_type, "message": str(exc)},
        headers=headers,
    )
