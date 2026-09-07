from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult
from app.services.github_provider import IntegrationProviderError

from ._compat import credential, mark_invalid_credential


@dataclass(frozen=True)
class GoogleCalendarProviderAdapter:
    provider_id = "google_calendar"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        row, secret = credential(self.compatibility_service, self.provider_id)
        try:
            output = self.compatibility_service.google_calendar.execute(
                invocation.operation, dict(invocation.input), secret
            )
        except IntegrationProviderError as exc:
            mark_invalid_credential(row, exc)
            raise
        finally:
            secret = ""
        resource = _audit_resource(invocation)
        if resource is None and isinstance(output.get("id"), str):
            resource = f"google-calendar-event:{output['id']}"
        return ProviderExecutionResult(output=output, audit_resource=resource)


def _audit_resource(invocation: AuthorizedIntegrationInvocation) -> str | None:
    resource = invocation.resource
    if resource is None:
        return None
    event_id = resource.values.get("event_id") or resource.values.get("id")
    return f"google-calendar-event:{event_id}" if event_id else None

