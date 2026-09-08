from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult


@dataclass(frozen=True)
class HuggingFaceProviderAdapter:
    provider_id = "huggingface"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        output = self.compatibility_service.huggingface.execute(
            invocation.operation,
            dict(invocation.input),
        )
        paper_id = invocation.input.get("paper_id")
        audit_resource = str(paper_id) if isinstance(paper_id, str) else None
        return ProviderExecutionResult(output=output, audit_resource=audit_resource)


__all__ = ["HuggingFaceProviderAdapter"]
