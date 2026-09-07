from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class IntegrationEffect(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    SEND = "send"
    EXECUTE = "execute"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ResourceSpec:
    type: str
    identity_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceIdentity:
    type: str
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class OperationPresentation:
    usage_example: dict[str, Any]
    normalized_errors: tuple[str, ...]
    open_world: bool = False


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display_name: str


@dataclass(frozen=True)
class IntegrationOperationSpec:
    id: str
    provider_id: str
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effects: frozenset[IntegrationEffect]
    resource: ResourceSpec
    risk: RiskLevel
    contract_version: int = 1
    presentation: OperationPresentation | None = None

    @property
    def requires_invocation_approval(self) -> bool:
        return self.risk is RiskLevel.HIGH

    @property
    def read_only(self) -> bool:
        return bool(self.effects) and self.effects <= {IntegrationEffect.READ}

    @property
    def destructive(self) -> bool:
        return IntegrationEffect.DELETE in self.effects

    def agent_context(self) -> dict[str, Any]:
        presentation = self.presentation
        return {
            "operation": self.id,
            "title": self.title,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "resource": {
                "type": self.resource.type,
                "identity_fields": list(self.resource.identity_fields),
            },
            "effects": sorted(effect.value for effect in self.effects),
            "read_only": self.read_only,
            "risk": self.risk.value,
            "requires_invocation_approval": self.requires_invocation_approval,
            "normalized_errors": list(presentation.normalized_errors) if presentation else [],
            "usage_example": presentation.usage_example if presentation else {},
            "helper": (
                "integration_runtime_capabilities.call(operation=..., input=...) for function or service code; "
                "web_runtime_capabilities.call_integration(operation=..., input=...) for web-app server code"
            ),
        }

    def contract_identity(self) -> dict[str, Any]:
        """Return the complete JSON-serializable semantic and security contract."""
        return {
            "provider": self.provider_id,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "effects": sorted(effect.value for effect in self.effects),
            "resource": {
                "type": self.resource.type,
                "identity_fields": list(self.resource.identity_fields),
            },
            "risk": self.risk.value,
            "version": self.contract_version,
        }


# Staged provider modules still import this name for annotations. It has no
# separate definition or authority.
IntegrationOperation = IntegrationOperationSpec
