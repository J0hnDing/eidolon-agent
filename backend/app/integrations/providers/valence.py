from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult


@dataclass(frozen=True)
class ValenceProviderAdapter:
    provider_id = "valence"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        output = self.compatibility_service.valence.execute(invocation.operation, dict(invocation.input))
        return ProviderExecutionResult(output=output, audit_resource=invocation.input.get("person_id"))
