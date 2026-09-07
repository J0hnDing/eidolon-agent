from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.runtime import ProviderExecutionResult
from app.services.atlas_knowledge_service import AtlasKnowledgeService


@dataclass(frozen=True)
class AtlasProviderAdapter:
    provider_id = "atlas"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        provider = self.compatibility_service.atlas
        operation_id = invocation.operation.id
        input_json = dict(invocation.input)
        if operation_id == "atlas.knowledge.node.know":
            inspected = provider.execute(
                DEFAULT_INTEGRATION_REGISTRY.get("atlas.knowledge.node.get"),
                {"node_id": input_json["node_id"]},
            )
            output = AtlasKnowledgeService(
                provider,
                adapter=self.compatibility_service.codex_adapter,
                project_root=self.compatibility_service.project_root,
            ).know(inspected["node"], input_json.get("explanation"))
        else:
            output = provider.execute(invocation.operation, input_json)
        return ProviderExecutionResult(output=output, audit_resource=_audit_resource(invocation))


def _audit_resource(invocation: AuthorizedIntegrationInvocation) -> str | None:
    resource = invocation.resource
    if resource is None:
        return None
    node_id = resource.values.get("node_id")
    return f"node:{node_id}" if node_id else None
