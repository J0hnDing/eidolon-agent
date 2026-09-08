from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator, ValidationError

from app.integrations.authorization import (
    AuthorizedIntegrationInvocation,
    is_authorized_integration_invocation,
)
from app.integrations.registry import IntegrationOperationRegistry
from app.services.atlas_knowledge_service import AtlasKnowledgeError
from app.services.github_provider import IntegrationProviderError
from app.services.telegram_provider import TelegramProviderError
from app.services.telegram_service import TelegramServiceError


@dataclass(frozen=True)
class ProviderExecutionResult:
    output: dict[str, Any] | list[Any]
    audit_resource: str | None = None


class ProviderAdapter(Protocol):
    provider_id: str

    def execute(
        self, invocation: AuthorizedIntegrationInvocation
    ) -> ProviderExecutionResult: ...


class IntegrationRuntimeError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retry_after = retry_after

    @property
    def retry_after_seconds(self) -> int | None:
        return self.retry_after


_PROVIDER_NAMES = {
    "github": "GitHub",
    "atlas": "Atlas",
    "notion": "Notion",
    "google_calendar": "Google Calendar",
    "gmail": "Gmail",
    "outlook": "Outlook",
    "telegram": "Telegram",
    "huggingface": "Hugging Face",
}

_SPECIAL_ERROR_MESSAGES = {
    "atlas_locked": "Atlas is locked",
    "node_already_known": "The selected Knowledge node is already known",
    "stale_revision": "The Knowledge node changed before it could be updated",
    "codex_unavailable": "A compatible Codex CLI is unavailable",
    "codex_failed": "Codex could not expand the Knowledge node",
    "schema_mismatch": "The provider resource does not match the required schema",
    "unsupported_file_type": "The requested provider file is not supported text",
    "operation_undeclared": "Integration operation is not implemented by its provider",
    "connection_unavailable": "Integration connection is unavailable",
    "invalid_input": "Integration input is invalid",
}


class IntegrationRuntime:
    """Dispatches policy-issued invocations through deterministic provider adapters."""

    def __init__(
        self,
        *,
        registry: IntegrationOperationRegistry,
        adapters: Iterable[ProviderAdapter],
    ) -> None:
        values: dict[str, ProviderAdapter] = {}
        for adapter in adapters:
            provider_id = adapter.provider_id
            if provider_id in values:
                raise ValueError(f"Duplicate integration provider adapter: {provider_id}")
            values[provider_id] = adapter
        self.registry = registry
        self.adapters = MappingProxyType(dict(sorted(values.items())))

    def execute(self, invocation: object) -> ProviderExecutionResult:
        if not is_authorized_integration_invocation(invocation):
            raise IntegrationRuntimeError(
                "authorization_missing_or_stale",
                "Integration invocation was not issued by backend authorization policy",
            )
        assert isinstance(invocation, AuthorizedIntegrationInvocation)
        canonical = self.registry.get(invocation.operation.id)
        if canonical is None or canonical != invocation.operation:
            raise IntegrationRuntimeError(
                "authorization_missing_or_stale",
                "Authorized integration operation is no longer current",
            )
        if (
            not self.registry.supports_provider(canonical.id, invocation.provider_id)
            or invocation.effects != canonical.effects
        ):
            raise IntegrationRuntimeError(
                "authorization_missing_or_stale",
                "Authorized integration security contract is inconsistent",
            )
        adapter = self.registry.resolve_adapter(invocation.provider_id, self.adapters)
        if adapter is None:
            raise IntegrationRuntimeError(
                "provider_unavailable", "Integration provider runtime is unavailable"
            )
        try:
            result = adapter.execute(invocation)
        except IntegrationRuntimeError:
            raise
        except IntegrationProviderError as exc:
            raise self._provider_error(invocation.provider_id, exc) from None
        except AtlasKnowledgeError as exc:
            raise IntegrationRuntimeError(
                exc.error_type,
                _SPECIAL_ERROR_MESSAGES.get(exc.error_type, "Atlas integration failed safely"),
            ) from None
        except (TelegramProviderError, TelegramServiceError) as exc:
            error_type = getattr(exc, "error_type", "provider_unavailable")
            raise IntegrationRuntimeError(
                str(error_type),
                self._safe_message("telegram", str(error_type)),
                retry_after=getattr(exc, "retry_after_seconds", None),
            ) from None
        except Exception:
            raise IntegrationRuntimeError(
                "internal_failure", "Integration provider failed safely"
            ) from None
        if not isinstance(result, ProviderExecutionResult) or not isinstance(result.output, (dict, list)):
            raise IntegrationRuntimeError(
                "internal_failure", "Integration returned an invalid normalized result"
            )
        # Email adapters return one provider slice. The invocation service
        # merges those slices, adds provider errors/mark outcomes, and then
        # validates the composite public envelope.
        if not canonical.id.startswith("email."):
            try:
                Draft202012Validator(canonical.output_schema).validate(result.output)
            except ValidationError:
                raise IntegrationRuntimeError(
                    "internal_failure", "Integration returned an invalid normalized result"
                ) from None
        return result

    @classmethod
    def _provider_error(
        cls, provider_id: str, error: IntegrationProviderError
    ) -> IntegrationRuntimeError:
        retry_after = error.retry_after_seconds
        message = cls._safe_message(provider_id, error.error_type)
        if error.error_type == "rate_limited" and retry_after is not None:
            message = f"{message}; retry after {retry_after} seconds"
        return IntegrationRuntimeError(
            error.error_type,
            message,
            retry_after=retry_after,
        )

    @staticmethod
    def _safe_message(provider_id: str, error_type: str) -> str:
        special = _SPECIAL_ERROR_MESSAGES.get(error_type)
        if special is not None:
            return special
        provider = _PROVIDER_NAMES.get(provider_id, "Integration provider")
        messages = {
            "invalid_credential": f"The {provider} authorization is invalid or revoked",
            "not_found": f"The requested {provider} resource was not found",
            "provider_forbidden": f"{provider} denied the requested operation",
            "rate_limited": f"{provider} rate limited the integration request",
            "provider_timeout": f"{provider} did not respond before the timeout",
            "response_too_large": f"{provider} response exceeded the operation limit",
            "provider_unavailable": f"{provider} is unavailable",
        }
        return messages.get(error_type, f"{provider} integration failed safely")


__all__ = [
    "IntegrationRuntime",
    "IntegrationRuntimeError",
    "ProviderAdapter",
    "ProviderExecutionResult",
]
