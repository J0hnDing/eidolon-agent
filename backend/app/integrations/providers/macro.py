from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult


@dataclass(frozen=True)
class MacroProviderAdapter:
    """Provider-neutral runtime bridge that keeps API keys in IntegrationService."""

    provider_id: str
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        output = self.compatibility_service.execute_macro(
            self.provider_id,
            invocation.operation,
            dict(invocation.input),
        )
        return ProviderExecutionResult(output=output)


__all__ = ["MacroProviderAdapter"]
