from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult
from app.services.whatsapp_service import WhatsAppService


@dataclass(frozen=True)
class WhatsAppProviderAdapter:
    provider_id = "whatsapp"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        result = WhatsAppService(
            self.compatibility_service.db,
            secret_store=self.compatibility_service.secret_store,
        ).execute(invocation)
        return ProviderExecutionResult(output=result, audit_resource=None)
