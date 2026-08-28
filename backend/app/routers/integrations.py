from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.integration import (
    GitHubConnectionStatus,
    GitHubCredentialWrite,
    IntegrationInvocationRequest,
    IntegrationInvocationResponse,
    NotionConnectionStatus,
    NotionCredentialWrite,
    NotionDataSourcesWrite,
)
from app.services.function_catalog_service import FunctionCatalogService
from app.services.function_registry_service import FunctionRegistryError, FunctionRegistryService
from app.services.integration_service import (
    IntegrationCaller,
    IntegrationError,
    build_default_integration_service,
)

router = APIRouter(tags=["integrations"])


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


def _http_error(exc: IntegrationError) -> HTTPException:
    status_code = {
        "invalid_input": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "invalid_credential": status.HTTP_401_UNAUTHORIZED,
        "provider_forbidden": status.HTTP_403_FORBIDDEN,
        "not_found": status.HTTP_404_NOT_FOUND,
        "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
        "provider_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
        "response_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
        "provider_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    }.get(exc.error_type, status.HTTP_409_CONFLICT)
    headers = (
        {"Retry-After": str(exc.retry_after_seconds)}
        if exc.error_type == "rate_limited" and exc.retry_after_seconds is not None
        else None
    )
    return HTTPException(
        status_code=status_code,
        detail={"type": exc.error_type, "message": str(exc)},
        headers=headers,
    )
