from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.models import IntegrationAuthorization, Skill
from app.schemas.manifest import ManifestIntegrationRequirement

from .types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    ResourceIdentity,
)

_AUTHORIZED_INVOCATION_MARKER = object()


@dataclass(frozen=True)
class ResourceScope:
    """A bounded provider-neutral resource grant.

    Existing non-GitHub integrations are intentionally represented as
    unrestricted because their containment is connection-owned. GitHub keeps
    its existing exact repository allow-list.
    """

    resource_type: str
    unrestricted: bool = False
    allowed: tuple[ResourceIdentity, ...] = ()

    def permits(self, resource: ResourceIdentity | None) -> bool:
        if self.unrestricted:
            return True
        if resource is None or resource.type != self.resource_type:
            return False
        expected = _identity_key(resource)
        return any(_identity_key(item) == expected for item in self.allowed)

    def contract_value(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "unrestricted": self.unrestricted,
            "allowed": [
                {key: value for key, value in sorted(item.values.items())}
                for item in sorted(self.allowed, key=_identity_key)
            ],
        }


@dataclass(frozen=True)
class AuthorizedIntegrationInvocation:
    """Secret-free request issued only after current backend authorization."""

    context: InvocationContext
    operation: IntegrationOperationSpec
    input: Mapping[str, Any]
    provider_id: str
    provider_account_id: str | None
    resource: ResourceIdentity | None
    effects: frozenset[IntegrationEffect]
    authorization_id: int | None
    _marker: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._marker is not _AUTHORIZED_INVOCATION_MARKER:
            raise TypeError("Authorized integration invocations are issued by backend policy")
        object.__setattr__(self, "input", MappingProxyType(dict(self.input)))


def _issue_authorized_integration_invocation(
    *,
    context: InvocationContext,
    operation: IntegrationOperationSpec,
    input_json: Mapping[str, Any],
    provider_account_id: str | None,
    resource: ResourceIdentity | None,
    authorization_id: int | None,
) -> AuthorizedIntegrationInvocation:
    return AuthorizedIntegrationInvocation(
        context=context,
        operation=operation,
        input=input_json,
        provider_id=operation.provider_id,
        provider_account_id=provider_account_id,
        resource=resource,
        effects=operation.effects,
        authorization_id=authorization_id,
        _marker=_AUTHORIZED_INVOCATION_MARKER,
    )


def is_authorized_integration_invocation(value: object) -> bool:
    return (
        isinstance(value, AuthorizedIntegrationInvocation)
        and value._marker is _AUTHORIZED_INVOCATION_MARKER
    )


def derive_resource_identity(
    operation: IntegrationOperationSpec,
    input_json: Mapping[str, Any],
) -> ResourceIdentity | None:
    fields = operation.resource.identity_fields
    if not fields or any(field not in input_json for field in fields):
        return None
    values: dict[str, str] = {}
    for field_name in fields:
        raw = input_json[field_name]
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            return None
        value = str(raw).strip()
        if not value:
            return None
        if operation.resource.type == "github.repository":
            value = value.lower()
        values[field_name] = value
    return ResourceIdentity(type=operation.resource.type, values=values)


class IntegrationAuthorizationService:
    """Standing delegated-authority fingerprints and typed resource scopes."""

    def __init__(self, db: Session, registry: Any) -> None:
        self.db = db
        self.registry = registry

    def resource_scope(
        self,
        requirement: ManifestIntegrationRequirement,
        operation: IntegrationOperationSpec,
    ) -> ResourceScope:
        if operation.resource.type == "github.repository":
            allowed = tuple(
                ResourceIdentity(
                    type="github.repository",
                    values={"owner": repository.split("/", 1)[0], "repository": repository.split("/", 1)[1]},
                )
                for repository in sorted(requirement.resource_scope.repositories)
            )
            return ResourceScope("github.repository", allowed=allowed)
        return ResourceScope(operation.resource.type, unrestricted=True)

    def requirement_scope_value(
        self,
        requirement: ManifestIntegrationRequirement,
    ) -> dict[str, Any]:
        scopes = {
            operation.resource.type: self.resource_scope(requirement, operation).contract_value()
            for operation_id in requirement.operations
            if (operation := self.registry.get(operation_id)) is not None
        }
        return {key: scopes[key] for key in sorted(scopes)}

    def contract_fingerprint(self, requirement: ManifestIntegrationRequirement) -> str:
        payload = {
            "provider": requirement.provider,
            "operations": sorted(requirement.operations),
            "resource_scope": self.requirement_scope_value(requirement),
            "operation_contracts": {
                operation_id: self.registry.get(operation_id).contract_identity()
                for operation_id in sorted(requirement.operations)
            },
        }
        return _fingerprint(payload)

    def legacy_contract_fingerprint(self, requirement: ManifestIntegrationRequirement) -> str:
        """Recognize and upgrade semantically equivalent pre-Phase-2 grants."""

        payload = {
            "provider": requirement.provider,
            "operations": sorted(requirement.operations),
            "resource_scope": {
                "repositories": sorted(requirement.resource_scope.repositories),
            },
            "registry_contract": {
                operation_id: {
                    "version": self.registry.get(operation_id).contract_version,
                    "requires_invocation_approval": self.registry.get(
                        operation_id
                    ).requires_invocation_approval,
                }
                for operation_id in sorted(requirement.operations)
            },
        }
        return _fingerprint(payload)

    def authorization(
        self,
        skill: Skill,
        requirement: ManifestIntegrationRequirement,
    ) -> IntegrationAuthorization | None:
        current_fingerprint = self.contract_fingerprint(requirement)
        authorization = self._for_fingerprint(
            skill,
            requirement.provider,
            current_fingerprint,
        )
        if authorization is not None:
            return authorization

        legacy = self._for_fingerprint(
            skill,
            requirement.provider,
            self.legacy_contract_fingerprint(requirement),
        )
        if legacy is None:
            return None
        # The active manifest is the source used to reconstruct the old scope;
        # only an exact old fingerprint is upgraded in place.
        legacy.contract_fingerprint = current_fingerprint
        self.db.commit()
        self.db.refresh(legacy)
        return legacy

    def authorization_state(
        self,
        skill: Skill,
        requirement: ManifestIntegrationRequirement,
    ) -> str:
        authorization = self.authorization(skill, requirement)
        if authorization is None:
            return "missing"
        status = authorization.approval_request.status
        return "stale" if status in {"expired", "superseded"} else status

    def _for_fingerprint(
        self,
        skill: Skill,
        provider: str,
        fingerprint: str,
    ) -> IntegrationAuthorization | None:
        return self.db.scalar(
            select(IntegrationAuthorization)
            .where(IntegrationAuthorization.skill_id == skill.id)
            .where(IntegrationAuthorization.provider == provider)
            .where(IntegrationAuthorization.contract_fingerprint == fingerprint)
            .where(IntegrationAuthorization.invalidated_at.is_(None))
            .order_by(IntegrationAuthorization.id.desc())
        )


def _identity_key(identity: ResourceIdentity) -> tuple[str, tuple[tuple[str, str], ...]]:
    return identity.type, tuple(sorted(identity.values.items()))


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
