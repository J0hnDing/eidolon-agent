from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.authorization import AuthorizedIntegrationInvocation
from app.integrations.runtime import ProviderExecutionResult
from app.services.github_provider import IntegrationProviderError
from app.services.report_service import ReportService
from app.services.todo_service import TodoService

from ._compat import credential, mark_invalid_credential


@dataclass(frozen=True)
class NotionProviderAdapter:
    provider_id = "notion"
    compatibility_service: Any

    def execute(self, invocation: AuthorizedIntegrationInvocation) -> ProviderExecutionResult:
        row, secret = credential(self.compatibility_service, self.provider_id)
        operation_id = invocation.operation.id
        input_json = dict(invocation.input)
        try:
            if operation_id.startswith("notion.report."):
                source_id = row.configured_report_resource_id
                factory = self.compatibility_service.notion_report_provider_factory
                if not source_id or factory is None:
                    raise IntegrationProviderError(
                        "connection_unavailable", "Notion Reports data source is unavailable"
                    )
                output = ReportService(factory(secret, source_id)).invoke(operation_id, input_json)
            else:
                source_id = row.configured_resource_id
                factory = self.compatibility_service.notion_provider_factory
                if not source_id or factory is None:
                    raise IntegrationProviderError(
                        "connection_unavailable", "Notion Todo data source is unavailable"
                    )
                output = TodoService(factory(secret, source_id)).invoke(operation_id, input_json)
        except IntegrationProviderError as exc:
            mark_invalid_credential(row, exc)
            raise
        finally:
            secret = ""
        return ProviderExecutionResult(output=output, audit_resource=_audit_resource(invocation))


def _audit_resource(invocation: AuthorizedIntegrationInvocation) -> str | None:
    resource = invocation.resource
    if resource is None:
        return None
    page_id = resource.values.get("id")
    return f"notion-page:{page_id}" if page_id else None

