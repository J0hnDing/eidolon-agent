from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult
from app.services.github_provider import IntegrationProviderError

from ._compat import credential, mark_invalid_credential


@dataclass(frozen=True)
class GoogleDriveProviderAdapter:
    provider_id = "google_drive"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        row, secret = credential(self.compatibility_service, self.provider_id)
        try:
            output = self.compatibility_service.google_drive.execute(invocation.operation.id, dict(invocation.input), secret)
        except IntegrationProviderError as exc:
            mark_invalid_credential(row, exc)
            raise
        finally:
            secret = ""
        resource = invocation.resource
        return ProviderExecutionResult(output=output, audit_resource=f"google-drive-file:{resource.values['file_id']}" if resource and resource.values.get("file_id") else None)
