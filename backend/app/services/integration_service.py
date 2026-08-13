from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ApprovalRequest,
    IntegrationAuditRecord,
    IntegrationAuthorization,
    IntegrationConnection,
    Skill,
)
from app.schemas.integration import GitHubConnectionStatus
from app.schemas.manifest import ManifestIntegrationRequirement, SkillManifest
from app.services.atlas_knowledge_service import AtlasKnowledgeError, AtlasKnowledgeService
from app.services.atlas_provider import AtlasProviderAdapter, UrllibAtlasProviderAdapter
from app.services.github_provider import (
    GitHubProviderAdapter,
    IntegrationProviderError,
    UrllibGitHubProviderAdapter,
)
from app.services.integration_registry import OPERATIONS, registry_contract_identity
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store


class IntegrationError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


PROVIDER_ERROR_MESSAGES = {
    "invalid_credential": "The GitHub credential is invalid or revoked",
    "not_found": "The requested GitHub resource was not found",
    "provider_forbidden": "GitHub denied the requested read operation",
    "rate_limited": "GitHub rate limited the integration request",
    "provider_timeout": "GitHub did not respond before the timeout",
    "response_too_large": "GitHub response exceeded the operation limit",
    "unsupported_file_type": "The requested GitHub file is not supported text",
    "provider_unavailable": "GitHub is unavailable",
    "internal_failure": "GitHub integration failed safely",
    "atlas_locked": "Atlas is locked",
    "node_already_known": "The selected Knowledge node is already known",
    "stale_revision": "The Knowledge node changed before it could be updated",
    "codex_unavailable": "A compatible Codex CLI is unavailable",
    "codex_failed": "Codex could not expand the Knowledge node",
}

ATLAS_PROVIDER_ERROR_MESSAGES = {
    **PROVIDER_ERROR_MESSAGES,
    "invalid_credential": "The Atlas API key is invalid",
    "not_found": "The requested Atlas item was not found",
    "provider_forbidden": "Atlas denied the requested operation",
    "provider_timeout": "Atlas did not respond before the timeout",
    "response_too_large": "Atlas response exceeded the operation limit",
    "provider_unavailable": "Atlas is unavailable",
    "internal_failure": "Atlas integration failed safely",
}
PROVIDER_DISPLAY_NAMES = {"github": "GitHub", "atlas": "Atlas"}


def provider_error_message(provider: str, error_type: str) -> str:
    messages = ATLAS_PROVIDER_ERROR_MESSAGES if provider == "atlas" else PROVIDER_ERROR_MESSAGES
    return messages.get(error_type, messages["internal_failure"])


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IntegrationCaller:
    skill_id: int
    version_id: int
    runtime: str
    skill_run_id: int | None = None
    web_app_instance_id: str | None = None


@dataclass
class IntegrationService:
    db: Session
    project_root: Path | None = None
    secret_store: SecretStore | None = None
    github: GitHubProviderAdapter | None = None
    atlas: AtlasProviderAdapter | None = None
    codex_adapter: Any | None = None

    def __post_init__(self) -> None:
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)
        self.project_root = self.proposed_service.project_root
        if self.github is None:
            self.github = UrllibGitHubProviderAdapter()
        if self.atlas is None:
            self.atlas = UrllibAtlasProviderAdapter()

    def connection_status(self) -> GitHubConnectionStatus:
        connection = self._connection()
        if connection is None:
            return GitHubConnectionStatus(connected=False, status="disconnected")
        available = self.secret_store is not None and self.secret_store.implementation_id == connection.secret_store_id
        status = connection.status if available else "unavailable"
        return GitHubConnectionStatus(
            connected=available and connection.status == "connected",
            status=status,
            account_login=connection.account_login,
            account_id=connection.account_id,
            last_validated_at=connection.last_validated_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            error_type=connection.error_type if status != "connected" else None,
        )

    def put_github_connection(self, credential: str) -> GitHubConnectionStatus:
        if not credential or len(credential) > 4096:
            raise IntegrationError("invalid_input", "GitHub credential must be a non-empty bounded string")
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            identity = self.github.validate_credential(credential)
        except IntegrationProviderError as exc:
            raise IntegrationError(
                exc.error_type,
                PROVIDER_ERROR_MESSAGES.get(exc.error_type, PROVIDER_ERROR_MESSAGES["internal_failure"]),
            ) from None
        try:
            new_reference = self.secret_store.put(credential)
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            credential = ""

        previous = self._connection()
        previous_reference = previous.secret_reference if previous is not None else None
        previous_account_id = previous.account_id if previous is not None else None
        now = utc_now()
        try:
            if previous is None:
                connection = IntegrationConnection(
                    provider="github",
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                    status="connected",
                    account_login=identity["login"],
                    account_id=identity["id"],
                    created_at=now,
                    updated_at=now,
                    last_validated_at=now,
                )
                self.db.add(connection)
            else:
                connection = previous
                connection.secret_store_id = self.secret_store.implementation_id
                connection.secret_reference = new_reference
                connection.status = "connected"
                connection.account_login = identity["login"]
                connection.account_id = identity["id"]
                connection.error_type = None
                connection.updated_at = now
                connection.last_validated_at = now
            if previous_account_id is not None and previous_account_id != identity["id"]:
                self.invalidate_provider_authorizations("github", "GitHub account identity changed")
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference)
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "GitHub connection could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference)
            except SecretStoreError:
                # The new active connection remains safe and usable. The stale
                # opaque entry is unreachable from Eidolon and contains no DB link.
                pass
        return self.connection_status()

    def remove_github_connection(self) -> GitHubConnectionStatus:
        connection = self._connection()
        if connection is None:
            return GitHubConnectionStatus(connected=False, status="disconnected")
        if (
            self.secret_store is None
            or self.secret_store.implementation_id != connection.secret_store_id
        ):
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(connection.secret_reference)
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage could not remove the credential") from None
        try:
            self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "GitHub connection could not be removed safely") from None
        return GitHubConnectionStatus(connected=False, status="disconnected")

    def ensure_authorization_requests(
        self,
        skill: Skill,
        manifest: SkillManifest,
        *,
        version_id: int | None = None,
    ) -> list[IntegrationAuthorization]:
        authorizations: list[IntegrationAuthorization] = []
        for requirement in manifest.integration_requirements:
            fingerprint = self.contract_fingerprint(requirement)
            current = self._authorization_for_fingerprint(
                skill,
                requirement.provider,
                fingerprint,
            )
            if current is not None:
                if current.approval_request.status in {"pending", "approved"}:
                    authorizations.append(current)
                    continue
                self._invalidate_authorization(current, "A new decision was requested for this integration contract")
            connection_available = self.provider_connected(requirement.provider)
            operations = [OPERATIONS[operation_id] for operation_id in requirement.operations]
            repositories = list(requirement.resource_scope.repositories)
            read_only = all(operation.read_only for operation in operations)
            provider_name = PROVIDER_DISPLAY_NAMES.get(requirement.provider, requirement.provider.title())
            action_description = "read-only access" if read_only else "read and bounded write access"
            mutation_detail = (
                " The Knowledge write uses one internet-enabled Codex call, writes one selected node, and may create "
                "immediate unassessed children through primitive Atlas operations; it cannot rename, move, delete, "
                "merge, or recursively expand nodes. The node update and each child creation are separately atomic."
                if any(operation.operation_id == "atlas.knowledge.node.know" for operation in operations)
                else ""
            )
            explanation = (
                f"Skill {skill.name} requests {action_description} to {provider_name} for "
                f"{', '.join(requirement.operations)}. "
                f"Resource scope: {', '.join(repositories) if repositories else 'provider-local only'}. "
                f"{provider_name} connection currently available: {'yes' if connection_available else 'no'}. "
                "Approval authorizes only this skill and unchanged integration contract. It does not reveal the "
                f"credential, grant direct network access, enable the skill, install it, or authorize future expansion.{mutation_detail}"
            )
            request = ApprovalRequest(
                skill_id=skill.id,
                request_scope="runtime",
                request_type="integration_access",
                risk_level="medium" if any(operation.risk == "medium" for operation in operations) else "low",
                requested_permissions_json={
                    "provider": requirement.provider,
                    "operations": list(requirement.operations),
                    "read_only": all(operation.read_only for operation in operations),
                    "resource_scope": {"repositories": repositories},
                },
                requested_dependencies_json=[],
                requested_network_domains_json=[],
                requested_filesystem_json={},
                reason_json={
                    "provider": requirement.provider,
                    "operations": list(requirement.operations),
                    "read_only": read_only,
                    "resource_scope": {"repositories": repositories},
                    "connection_available": connection_available,
                    "contract_fingerprint": fingerprint,
                    "version_id": version_id,
                    "approval_means": "This skill may call only these backend-controlled selected operations.",
                    "approval_does_not_mean": [
                        f"the {provider_name} credential is shared with the skill",
                        f"direct {provider_name} or general network access is allowed",
                        "new operations or repositories are approved",
                        "the skill is installed, enabled, scheduled, or run",
                    ],
                },
                reason=explanation,
                user_explanation=explanation,
                status="pending",
            )
            self.db.add(request)
            self.db.flush()
            authorization = IntegrationAuthorization(
                skill_id=skill.id,
                provider=requirement.provider,
                contract_fingerprint=fingerprint,
                approval_request_id=request.id,
            )
            self.db.add(authorization)
            self.db.commit()
            self.db.refresh(authorization)
            authorizations.append(authorization)
        return authorizations

    def authorization_state(self, skill: Skill, requirement: ManifestIntegrationRequirement) -> str:
        authorization = self._authorization_for_fingerprint(
            skill,
            requirement.provider,
            self.contract_fingerprint(requirement),
        )
        if authorization is None:
            return "missing"
        status = authorization.approval_request.status
        return "stale" if status in {"expired", "superseded"} else status

    def integration_review(self, skill: Skill, manifest: SkillManifest) -> list[dict[str, Any]]:
        return [
            {
                "provider": requirement.provider,
                "operations": list(requirement.operations),
                "read_only": all(OPERATIONS[operation_id].read_only for operation_id in requirement.operations),
                "resource_scope": requirement.resource_scope.model_dump(mode="json"),
                "connection_available": self.provider_connected(requirement.provider),
                "authorization_state": self.authorization_state(skill, requirement),
            }
            for requirement in manifest.integration_requirements
        ]

    def invoke(
        self,
        caller: IntegrationCaller,
        operation_id: str,
        input_json: dict[str, Any],
    ) -> dict[str, Any]:
        skill = self.db.get(Skill, caller.skill_id)
        if skill is None:
            raise IntegrationError("connection_unavailable", "Integration caller no longer exists")
        if skill.active_version_id != caller.version_id:
            raise IntegrationError("authorization_missing_or_stale", "Integration caller version is stale")
        audit = IntegrationAuditRecord(
            skill_id=skill.id,
            version_id=caller.version_id,
            skill_run_id=caller.skill_run_id,
            web_app_instance_id=caller.web_app_instance_id,
            operation_id=operation_id,
            status="running",
            request_size=len(json.dumps(input_json, separators=(",", ":")).encode("utf-8")),
            started_at=utc_now(),
        )
        self.db.add(audit)
        self._commit_audit()
        try:
            output = self._invoke_checked(skill, caller, operation_id, input_json, audit)
            audit.status = "succeeded"
            audit.response_size = len(json.dumps(output, separators=(",", ":")).encode("utf-8"))
            audit.completed_at = utc_now()
            self._commit_audit()
            return output
        except IntegrationError as exc:
            audit.status = "failed"
            audit.error_type = exc.error_type
            audit.completed_at = utc_now()
            self._commit_audit()
            raise
        except Exception:
            audit.status = "failed"
            audit.error_type = "internal_failure"
            audit.completed_at = utc_now()
            self._commit_audit()
            raise IntegrationError("internal_failure", "Integration failed safely") from None

    def _invoke_checked(
        self,
        skill: Skill,
        caller: IntegrationCaller,
        operation_id: str,
        input_json: dict[str, Any],
        audit: IntegrationAuditRecord,
    ) -> dict[str, Any]:
        # The order is deliberate: the credential is retrieved only after every
        # caller, manifest, approval, connection, scope, and schema check passes.
        if skill.status != "installed" or not skill.enabled:
            raise IntegrationError("authorization_missing_or_stale", "Integration caller is not installed and enabled")
        if skill.runtime != caller.runtime or caller.runtime not in {"function", "web_app"}:
            raise IntegrationError("authorization_missing_or_stale", "Integration caller runtime is not eligible")
        try:
            manifest = validate_manifest_file(self.proposed_service.skill_dir_for_record(skill) / "manifest.json")
        except (ManifestValidationError, ProposedSkillError, FileNotFoundError):
            raise IntegrationError("authorization_missing_or_stale", "Integration caller manifest is invalid") from None
        from app.services.permission_service import PermissionService

        runtime_permissions = PermissionService(self.db, project_root=self.project_root).can_run(
            skill,
            include_integrations=False,
        )
        if not runtime_permissions.allowed:
            raise IntegrationError("authorization_missing_or_stale", "Runtime permission approval is missing or stale")
        requirement = next(
            (item for item in manifest.integration_requirements if operation_id in item.operations),
            None,
        )
        if requirement is None:
            raise IntegrationError("operation_undeclared", "Integration operation is not declared by the active manifest")
        if self.authorization_state(skill, requirement) != "approved":
            raise IntegrationError("authorization_missing_or_stale", "Integration authorization is missing or stale")
        operation = OPERATIONS.get(operation_id)
        if operation is None:
            raise IntegrationError("operation_undeclared", "Integration operation does not exist")
        connection = self._connection(operation.provider)
        if connection is None or connection.status != "connected":
            provider_name = PROVIDER_DISPLAY_NAMES.get(operation.provider, operation.provider.title())
            raise IntegrationError("connection_unavailable", f"{provider_name} connection is unavailable")
        scoped_resource = self._resource(operation.resource_scope, input_json)
        resource = scoped_resource
        if resource is None and "node_id" in operation.audit_resource_fields:
            node_id = input_json.get("node_id")
            if isinstance(node_id, int) and not isinstance(node_id, bool):
                resource = f"node:{node_id}"
        audit.resource = resource
        if scoped_resource is not None and scoped_resource not in requirement.resource_scope.repositories:
            raise IntegrationError("repository_outside_scope", "GitHub repository is outside the approved scope")
        try:
            Draft202012Validator(operation.input_schema).validate(input_json)
        except ValidationError as exc:
            path = ".".join(str(item) for item in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise IntegrationError("invalid_input", f"Integration input is invalid{location}") from None
        if (
            self.secret_store is None
            or self.secret_store.implementation_id != connection.secret_store_id
        ):
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            if operation.provider == "atlas":
                from app.services.atlas_settings_service import AtlasSettingsService

                credential = AtlasSettingsService(self.db, secret_store=self.secret_store).api_key()
            else:
                credential = self.secret_store.get(connection.secret_reference)
        except (SecretStoreError, RuntimeError):
            raise IntegrationError("connection_unavailable", "Stored integration credential is unavailable") from None
        try:
            try:
                if operation.operation_id == "atlas.knowledge.node.know":
                    inspected = self.atlas.execute(OPERATIONS["atlas.knowledge.node.get"], {"node_id": input_json["node_id"]}, credential)
                    output = AtlasKnowledgeService(
                        self.atlas,
                        adapter=self.codex_adapter,
                        project_root=self.project_root,
                    ).know(inspected["node"], input_json.get("explanation"), credential)
                elif operation.provider == "atlas":
                    output = self.atlas.execute(operation, input_json, credential)
                else:
                    output = self.github.execute(operation, input_json, credential)
            except IntegrationProviderError as exc:
                if exc.error_type == "invalid_credential":
                    connection.status = "invalid"
                    connection.error_type = "invalid_credential"
                raise IntegrationError(
                    exc.error_type,
                    provider_error_message(operation.provider, exc.error_type),
                ) from None
            except AtlasKnowledgeError as exc:
                raise IntegrationError(exc.error_type, str(exc)) from None
        finally:
            credential = ""
        try:
            Draft202012Validator(operation.output_schema).validate(output)
        except ValidationError:
            raise IntegrationError("internal_failure", "Integration returned an invalid normalized result") from None
        return output

    def _commit_audit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Integration audit failed safely") from None

    def contract_fingerprint(self, requirement: ManifestIntegrationRequirement) -> str:
        payload = {
            "provider": requirement.provider,
            "operations": sorted(requirement.operations),
            "resource_scope": {
                "repositories": sorted(requirement.resource_scope.repositories),
            },
            "registry_contract": registry_contract_identity(requirement.operations),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def invalidate_provider_authorizations(self, provider: str, reason: str) -> None:
        authorizations = self.db.scalars(
            select(IntegrationAuthorization)
            .where(IntegrationAuthorization.provider == provider)
            .where(IntegrationAuthorization.invalidated_at.is_(None))
        ).all()
        for authorization in authorizations:
            self._invalidate_authorization(authorization, reason, commit=False)

    def _connection(self, provider: str = "github") -> IntegrationConnection | None:
        return self.db.scalar(
            select(IntegrationConnection).where(IntegrationConnection.provider == provider)
        )

    def provider_connected(self, provider: str) -> bool:
        connection = self._connection(provider)
        if connection is None or connection.status != "connected":
            return False
        if self.secret_store is None or self.secret_store.implementation_id != connection.secret_store_id:
            return False
        if provider == "atlas":
            try:
                from app.services.atlas_settings_service import AtlasSettingsService

                status = AtlasSettingsService(self.db, secret_store=self.secret_store).status()
                return bool(status.running and status.locked is False and status.api_key_status == "connected")
            except (AttributeError, ImportError, RuntimeError):
                return False
        return True

    def _authorization_for_fingerprint(
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
            .order_by(IntegrationAuthorization.created_at.desc(), IntegrationAuthorization.id.desc())
        )

    def _invalidate_authorization(
        self,
        authorization: IntegrationAuthorization,
        reason: str,
        *,
        commit: bool = True,
    ) -> None:
        authorization.invalidated_at = utc_now()
        authorization.invalidation_reason = reason
        if authorization.approval_request.status in {"pending", "approved"}:
            authorization.approval_request.status = "superseded"
        if commit:
            self.db.commit()

    @staticmethod
    def _resource(scope_behavior: str, input_json: dict[str, Any]) -> str | None:
        if scope_behavior != "repository":
            return None
        owner = input_json.get("owner")
        repository = input_json.get("repository")
        if not isinstance(owner, str) or not isinstance(repository, str):
            return None
        return f"{owner.lower()}/{repository.lower()}"


def build_default_integration_service(db: Session) -> IntegrationService:
    try:
        store = default_secret_store()
    except SecretStoreError:
        store = None
    return IntegrationService(db, secret_store=store)
