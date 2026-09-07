from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult
from app.services.telegram_service import TelegramService


@dataclass(frozen=True)
class TelegramProviderAdapter:
    provider_id = "telegram"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        output = TelegramService(
            self.compatibility_service.db,
            secret_store=self.compatibility_service.secret_store,
        ).execute_notification(dict(invocation.input))
        return ProviderExecutionResult(output=output, audit_resource=None)

