from __future__ import annotations

from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult
from app.services.email_contracts import normalize_provider_output
from app.services.github_provider import IntegrationProviderError

from ._compat import credential, mark_invalid_credential


class OutlookProviderAdapter:
    provider_id = "outlook"

    def __init__(self, compatibility_service: Any) -> None:
        self.compatibility_service = compatibility_service

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        row, secret = credential(self.compatibility_service, self.provider_id)
        try:
            output = self.compatibility_service.outlook.execute(
                invocation.operation,
                dict(invocation.input),
                secret,
            )
        except IntegrationProviderError as exc:
            mark_invalid_credential(row, exc)
            raise
        finally:
            secret = ""
        return ProviderExecutionResult(
            output=normalize_provider_output(output, provider=self.provider_id),
            audit_resource=_audit_resource(invocation),
        )


def _audit_resource(invocation: AuthorizedIntegrationInvocation) -> str | None:
    resource = invocation.resource
    if resource is None:
        return None
    conversation_id = resource.values.get("conversation_id")
    message_id = resource.values.get("id")
    if conversation_id:
        return f"outlook-conversation:{conversation_id}"
    return f"outlook-message:{message_id}" if message_id else None


__all__ = ["OutlookProviderAdapter"]
