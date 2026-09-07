from __future__ import annotations

from typing import Any

from app.models import IntegrationConnection


class IntegrationConnectionService:
    """Connection-state boundary used by policy and provider adapters.

    Settings and OAuth routes may continue to use the compatibility façade;
    catalog execution receives only this connection-focused view.
    """

    def __init__(self, compatibility_service: Any) -> None:
        self._compatibility_service = compatibility_service

    @property
    def secret_store(self):
        return self._compatibility_service.secret_store

    def operation_available(self, operation_id: str) -> bool:
        return self._compatibility_service.operation_available(operation_id)

    def provider_connected(self, provider_id: str) -> bool:
        return self._compatibility_service.provider_connected(provider_id)

    def connection(self, provider_id: str) -> IntegrationConnection | None:
        return self._compatibility_service._connection(provider_id)

    def google_runtime_credential(self, service_credential: str) -> str:
        return self._compatibility_service._google_runtime_credential(service_credential)

