from __future__ import annotations

from typing import Any

from app.services.github_provider import IntegrationProviderError
from app.services.secret_store import SecretStoreError

_NAMESPACES = {
    "github": "github",
    "notion": "notion",
    "google_calendar": "google_calendar",
    "gmail": "gmail",
}


def connection(compatibility_service: Any, provider_id: str) -> Any:
    row = compatibility_service._connection(provider_id)  # noqa: SLF001 - staged composition boundary
    if row is None:
        raise IntegrationProviderError(
            "connection_unavailable", "Integration connection is unavailable"
        )
    return row


def credential(compatibility_service: Any, provider_id: str) -> tuple[Any, str]:
    row = connection(compatibility_service, provider_id)
    store = compatibility_service.secret_store
    if store is None or store.implementation_id != row.secret_store_id:
        raise IntegrationProviderError(
            "connection_unavailable", "Operating-system secret storage is unavailable"
        )
    try:
        value = store.get(row.secret_reference, namespace=_NAMESPACES[provider_id])
    except (SecretStoreError, RuntimeError):
        raise IntegrationProviderError(
            "connection_unavailable", "Stored integration credential is unavailable"
        ) from None
    if provider_id in {"google_calendar", "gmail"}:
        try:
            value = compatibility_service._google_runtime_credential(value)  # noqa: SLF001
        except Exception as exc:
            error_type = getattr(exc, "error_type", "connection_unavailable")
            raise IntegrationProviderError(
                str(error_type), "Stored Google authorization is unavailable"
            ) from None
    return row, value


def mark_invalid_credential(row: Any, error: IntegrationProviderError) -> None:
    if error.error_type != "invalid_credential":
        return
    row.status = "invalid"
    row.error_type = "invalid_credential"

