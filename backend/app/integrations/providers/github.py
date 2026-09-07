from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult
from app.services.github_provider import IntegrationProviderError

from ._compat import credential, mark_invalid_credential


@dataclass(frozen=True)
class GitHubProviderAdapter:
    provider_id = "github"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        row, secret = credential(self.compatibility_service, self.provider_id)
        try:
            output = self.compatibility_service.github.execute(
                invocation.operation, dict(invocation.input), secret
            )
        except IntegrationProviderError as exc:
            mark_invalid_credential(row, exc)
            raise
        finally:
            secret = ""
        return ProviderExecutionResult(output=output, audit_resource=_audit_resource(invocation))


def _audit_resource(invocation: AuthorizedIntegrationInvocation) -> str | None:
    resource = invocation.resource
    if resource is None or resource.type != "github.repository":
        return None
    owner = resource.values.get("owner")
    repository = resource.values.get("repository")
    return f"{owner}/{repository}" if owner and repository else None

