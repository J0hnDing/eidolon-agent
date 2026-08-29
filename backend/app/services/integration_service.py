from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

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
from app.schemas.integration import GitHubConnectionStatus, GoogleCalendarConnectionStatus, NotionConnectionStatus
from app.schemas.manifest import ManifestIntegrationRequirement, SkillManifest
from app.services.atlas_knowledge_service import AtlasKnowledgeError, AtlasKnowledgeService
from app.services.atlas_provider import AtlasProviderAdapter, UrllibAtlasProviderAdapter
from app.services.github_provider import (
    GitHubProviderAdapter,
    IntegrationProviderError,
    UrllibGitHubProviderAdapter,
)
from app.services.google_calendar_provider import (
    GOOGLE_CALENDAR_SECRET_NAMESPACE,
    GOOGLE_OAUTH_REDIRECT_URI,
    GoogleCalendarProviderAdapter,
    GoogleOAuthStateStore,
    UrllibGoogleCalendarProviderAdapter,
    google_oauth_state_store,
)
from app.services.integration_registry import OPERATIONS, registry_contract_identity
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file
from app.services.notion_report_provider import NotionReportProvider
from app.services.notion_todo_provider import NotionTodoProvider
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.report_service import ReportProvider, ReportService
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store
from app.services.todo_service import TodoProvider, TodoService


class IntegrationError(RuntimeError):
    def __init__(self, error_type: str, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retry_after_seconds = retry_after_seconds


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
    **{key: value for key, value in PROVIDER_ERROR_MESSAGES.items() if key != "invalid_credential"},
    "not_found": "The requested Atlas item was not found",
    "provider_forbidden": "Atlas denied the requested operation",
    "provider_timeout": "Atlas did not respond before the timeout",
    "response_too_large": "Atlas response exceeded the operation limit",
    "provider_unavailable": "Atlas is unavailable",
    "internal_failure": "Atlas integration failed safely",
}
NOTION_PROVIDER_ERROR_MESSAGES = {
    "invalid_credential": "The Notion credential is invalid or revoked",
    "not_found": "The requested Notion resource was not found",
    "schema_mismatch": "The Notion data source or row does not match the required schema",
    "provider_forbidden": "Notion denied the requested operation",
    "rate_limited": "Notion rate limited the integration request",
    "provider_timeout": "Notion did not respond before the timeout",
    "response_too_large": "Notion response exceeded the operation limit",
    "provider_unavailable": "Notion is unavailable",
    "internal_failure": "Notion integration failed safely",
}
GOOGLE_CALENDAR_PROVIDER_ERROR_MESSAGES = {
    "invalid_credential": "The Google Calendar authorization is invalid or revoked",
    "not_found": "The requested Google Calendar event was not found",
    "provider_forbidden": "Google denied the requested Calendar operation",
    "rate_limited": "Google Calendar rate limited the integration request",
    "provider_timeout": "Google Calendar did not respond before the timeout",
    "response_too_large": "Google Calendar response exceeded the operation limit",
    "provider_unavailable": "Google Calendar is unavailable",
    "internal_failure": "Google Calendar integration failed safely",
}
PROVIDER_DISPLAY_NAMES = {
    "github": "GitHub",
    "atlas": "Atlas",
    "notion": "Notion",
    "google_calendar": "Google Calendar",
}


def provider_error_message(provider: str, error_type: str) -> str:
    if provider == "atlas":
        messages = ATLAS_PROVIDER_ERROR_MESSAGES
    elif provider == "notion":
        messages = NOTION_PROVIDER_ERROR_MESSAGES
    elif provider == "google_calendar":
        messages = GOOGLE_CALENDAR_PROVIDER_ERROR_MESSAGES
    else:
        messages = PROVIDER_ERROR_MESSAGES
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
    notion_provider_factory: Callable[[str, str], TodoProvider] | None = None
    notion_report_provider_factory: Callable[[str, str], ReportProvider] | None = None
    google_calendar: GoogleCalendarProviderAdapter | None = None
    google_oauth_states: GoogleOAuthStateStore | None = None
    codex_adapter: Any | None = None

    def __post_init__(self) -> None:
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)
        self.project_root = self.proposed_service.project_root
        if self.github is None:
            self.github = UrllibGitHubProviderAdapter()
        if self.atlas is None:
            self.atlas = UrllibAtlasProviderAdapter()
        if self.notion_provider_factory is None:
            self.notion_provider_factory = NotionTodoProvider
        if self.notion_report_provider_factory is None:
            self.notion_report_provider_factory = NotionReportProvider
        if self.google_calendar is None:
            self.google_calendar = UrllibGoogleCalendarProviderAdapter()
        if self.google_oauth_states is None:
            self.google_oauth_states = google_oauth_state_store

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

    def google_calendar_connection_status(self) -> GoogleCalendarConnectionStatus:
        connection = self._connection("google_calendar")
        if connection is None:
            return GoogleCalendarConnectionStatus(
                connected=False,
                status="disconnected",
                oauth_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            )
        available = self.secret_store is not None and self.secret_store.implementation_id == connection.secret_store_id
        status = connection.status if available else "unavailable"
        return GoogleCalendarConnectionStatus(
            connected=available and connection.status == "connected",
            status=status,
            account_email=connection.account_login,
            last_validated_at=connection.last_validated_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            error_type=connection.error_type if status != "connected" else None,
            oauth_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
        )

    def start_google_calendar_oauth(self, client_id: str, client_secret: str) -> str:
        if not 1 <= len(client_id) <= 1024 or not 1 <= len(client_secret) <= 4096:
            raise IntegrationError("invalid_input", "Google OAuth client credentials must be non-empty bounded strings")
        assert self.google_oauth_states is not None
        assert self.google_calendar is not None
        state = self.google_oauth_states.create(client_id, client_secret)
        return self.google_calendar.authorization_url(client_id, state)

    def discard_google_calendar_oauth(self, state: str) -> None:
        if not 1 <= len(state) <= 512:
            raise IntegrationError("invalid_credential", "Google OAuth response is invalid or expired")
        assert self.google_oauth_states is not None
        try:
            self.google_oauth_states.consume(state)
        except IntegrationProviderError as exc:
            raise IntegrationError(exc.error_type, provider_error_message("google_calendar", exc.error_type)) from None

    def complete_google_calendar_oauth(self, state: str, code: str) -> GoogleCalendarConnectionStatus:
        if not 1 <= len(state) <= 512 or not 1 <= len(code) <= 8192:
            raise IntegrationError("invalid_credential", "Google OAuth response is invalid or expired")
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        assert self.google_oauth_states is not None
        assert self.google_calendar is not None
        try:
            pending = self.google_oauth_states.consume(state)
            tokens = self.google_calendar.exchange_code(pending, code)
            identity = self.google_calendar.identity(tokens["access_token"])
        except IntegrationProviderError as exc:
            raise IntegrationError(exc.error_type, provider_error_message("google_calendar", exc.error_type)) from None
        credential = json.dumps(
            {
                "client_id": pending.client_id,
                "client_secret": pending.client_secret,
                "refresh_token": tokens["refresh_token"],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            new_reference = self.secret_store.put(
                credential,
                namespace=GOOGLE_CALENDAR_SECRET_NAMESPACE,
            )
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            credential = ""
            tokens = {}

        previous = self._connection("google_calendar")
        previous_reference = previous.secret_reference if previous is not None else None
        previous_account_id = previous.account_id if previous is not None else None
        now = utc_now()
        try:
            if previous is None:
                connection = IntegrationConnection(
                    provider="google_calendar",
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                    credential_kind="oauth_refresh",
                    status="connected",
                    account_login=identity["email"],
                    account_id=identity["account_id"],
                    created_at=now,
                    updated_at=now,
                    last_validated_at=now,
                )
                self.db.add(connection)
            else:
                connection = previous
                connection.secret_store_id = self.secret_store.implementation_id
                connection.secret_reference = new_reference
                connection.credential_kind = "oauth_refresh"
                connection.status = "connected"
                connection.account_login = identity["email"]
                connection.account_id = identity["account_id"]
                connection.error_type = None
                connection.updated_at = now
                connection.last_validated_at = now
            if previous_account_id is not None and previous_account_id != identity["account_id"]:
                self.invalidate_provider_authorizations(
                    "google_calendar",
                    "Google Calendar account identity changed",
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace=GOOGLE_CALENDAR_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Google Calendar connection could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference, namespace=GOOGLE_CALENDAR_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        return self.google_calendar_connection_status()

    def notion_connection_status(self) -> NotionConnectionStatus:
        connection = self._connection("notion")
        if connection is None:
            return NotionConnectionStatus(connected=False, status="disconnected")
        available = self.secret_store is not None and self.secret_store.implementation_id == connection.secret_store_id
        status = connection.status if available else "unavailable"
        return NotionConnectionStatus(
            connected=available and connection.status == "connected",
            status=status,
            bot_name=connection.account_login or None,
            bot_id=connection.account_id or None,
            workspace_name=connection.workspace_name,
            data_source_id=connection.configured_resource_id,
            report_data_source_id=connection.configured_report_resource_id,
            last_validated_at=connection.last_validated_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            error_type=connection.error_type if status != "connected" else None,
        )

    def put_notion_credential(self, credential: str) -> NotionConnectionStatus:
        if not credential or len(credential) > 4096:
            raise IntegrationError("invalid_input", "Notion token must be a non-empty bounded string")
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        assert self.notion_provider_factory is not None
        assert self.notion_report_provider_factory is not None
        previous = self._connection("notion")
        todo_source = previous.configured_resource_id if previous is not None else None
        report_source = previous.configured_report_resource_id if previous is not None else None
        todo_provider = self.notion_provider_factory(credential, todo_source or "identity-only")
        validation_target = "connection"
        try:
            validation_target = "Todo data source" if todo_source else "connection"
            identity = (
                todo_provider.validate_connection()
                if todo_source
                else todo_provider.validate_identity()
            )
            if report_source:
                validation_target = "Reports data source"
                report_identity = self.notion_report_provider_factory(
                    credential,
                    report_source,
                ).validate_connection()
                report_bot_id, _report_bot_name, _report_workspace = (
                    self._validated_notion_identity(report_identity)
                )
                if report_bot_id != identity.get("bot_id"):
                    raise IntegrationError(
                        "provider_unavailable",
                        "Notion data sources resolved to different bot identities",
                    )
        except IntegrationProviderError as exc:
            raise self._notion_validation_error(validation_target, exc) from None
        bot_id, bot_name, workspace_name = self._validated_notion_identity(identity)
        try:
            new_reference = self.secret_store.put(credential, namespace="notion")
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            credential = ""

        previous_reference = previous.secret_reference if previous is not None else None
        identity_changed = previous is not None and previous.account_id != bot_id
        now = utc_now()
        try:
            if previous is None:
                connection = IntegrationConnection(
                    provider="notion",
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                    status="connected",
                    account_login=bot_name,
                    account_id=bot_id,
                    workspace_name=workspace_name,
                    configured_resource_id=None,
                    configured_report_resource_id=None,
                    created_at=now,
                    updated_at=now,
                    last_validated_at=now,
                )
                self.db.add(connection)
            else:
                previous.secret_store_id = self.secret_store.implementation_id
                previous.secret_reference = new_reference
                previous.status = "connected"
                previous.account_login = bot_name
                previous.account_id = bot_id
                previous.workspace_name = workspace_name
                previous.error_type = None
                previous.updated_at = now
                previous.last_validated_at = now
            if identity_changed:
                self.invalidate_provider_authorizations("notion", "Notion identity changed")
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace="notion")
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Notion connection could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference, namespace="notion")
            except SecretStoreError:
                pass
        return self.notion_connection_status()

    def put_notion_data_sources(
        self,
        data_source_id: str,
        report_data_source_id: str,
    ) -> NotionConnectionStatus:
        normalized_source = data_source_id.strip()
        normalized_report_source = report_data_source_id.strip()
        if (
            not normalized_source
            or len(normalized_source) > 256
            or not normalized_report_source
            or len(normalized_report_source) > 256
        ):
            raise IntegrationError(
                "invalid_input",
                "Both Notion data-source IDs must be non-empty bounded strings",
            )
        connection = self._connected_notion_connection()
        assert self.secret_store is not None
        try:
            credential = self.secret_store.get(connection.secret_reference, namespace="notion")
        except (SecretStoreError, RuntimeError):
            raise IntegrationError("connection_unavailable", "Stored Notion credential is unavailable") from None
        assert self.notion_provider_factory is not None
        assert self.notion_report_provider_factory is not None
        validation_target = "Todo data source"
        try:
            identity = self.notion_provider_factory(
                credential,
                normalized_source,
            ).validate_connection()
            validation_target = "Reports data source"
            report_identity = self.notion_report_provider_factory(
                credential,
                normalized_report_source,
            ).validate_connection()
        except IntegrationProviderError as exc:
            raise self._notion_validation_error(validation_target, exc) from None
        finally:
            credential = ""
        bot_id, _bot_name, _workspace_name = self._validated_notion_identity(identity)
        report_bot_id, _report_bot_name, _report_workspace = self._validated_notion_identity(
            report_identity
        )
        if report_bot_id != bot_id or connection.account_id != bot_id:
            raise IntegrationError(
                "provider_unavailable",
                "Notion data sources do not match the connected bot identity",
            )
        try:
            connection.configured_resource_id = normalized_source
            connection.configured_report_resource_id = normalized_report_source
            connection.error_type = None
            connection.updated_at = utc_now()
            connection.last_validated_at = connection.updated_at
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Notion data sources could not be saved safely") from None
        return self.notion_connection_status()

    def remove_notion_data_sources(self) -> NotionConnectionStatus:
        connection = self._connection("notion")
        if connection is None:
            return NotionConnectionStatus(connected=False, status="disconnected")
        try:
            connection.configured_resource_id = None
            connection.configured_report_resource_id = None
            connection.updated_at = utc_now()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Notion data sources could not be removed safely") from None
        return self.notion_connection_status()

    def put_notion_connection(
        self,
        credential: str,
        data_source_id: str,
        report_data_source_id: str,
    ) -> NotionConnectionStatus:
        normalized_source = data_source_id.strip()
        normalized_report_source = report_data_source_id.strip()
        if (
            not credential
            or len(credential) > 4096
            or not normalized_source
            or len(normalized_source) > 256
            or not normalized_report_source
            or len(normalized_report_source) > 256
        ):
            raise IntegrationError(
                "invalid_input",
                "Notion token and both data-source IDs must be non-empty bounded strings",
            )
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        assert self.notion_provider_factory is not None
        assert self.notion_report_provider_factory is not None
        provider = self.notion_provider_factory(credential, normalized_source)
        report_provider = self.notion_report_provider_factory(credential, normalized_report_source)
        try:
            identity = provider.validate_connection()
            report_identity = report_provider.validate_connection()
        except IntegrationProviderError as exc:
            raise IntegrationError(
                exc.error_type,
                provider_error_message("notion", exc.error_type),
                retry_after_seconds=exc.retry_after_seconds,
            ) from None
        bot_id = str(identity.get("bot_id") or "")
        report_bot_id = str(report_identity.get("bot_id") or "")
        if not bot_id or len(bot_id) > 128:
            raise IntegrationError("provider_unavailable", "Notion returned an invalid bot identity")
        if report_bot_id != bot_id:
            raise IntegrationError("provider_unavailable", "Notion data sources resolved to different bot identities")
        bot_name = self._sanitized_identity(identity.get("bot_name"), 128) or "Notion bot"
        workspace_name = self._sanitized_identity(identity.get("workspace_name"), 256)
        try:
            new_reference = self.secret_store.put(credential, namespace="notion")
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            credential = ""

        previous = self._connection("notion")
        previous_reference = previous.secret_reference if previous is not None else None
        identity_changed = previous is not None and previous.account_id != bot_id
        now = utc_now()
        try:
            if previous is None:
                connection = IntegrationConnection(
                    provider="notion",
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                    status="connected",
                    account_login=bot_name,
                    account_id=bot_id,
                    workspace_name=workspace_name,
                    configured_resource_id=normalized_source,
                    configured_report_resource_id=normalized_report_source,
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
                connection.account_login = bot_name
                connection.account_id = bot_id
                connection.workspace_name = workspace_name
                connection.configured_resource_id = normalized_source
                connection.configured_report_resource_id = normalized_report_source
                connection.error_type = None
                connection.updated_at = now
                connection.last_validated_at = now
            if identity_changed:
                self.invalidate_provider_authorizations("notion", "Notion identity changed")
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace="notion")
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Notion connection could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference, namespace="notion")
            except SecretStoreError:
                pass
        return self.notion_connection_status()

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

    def remove_notion_connection(self) -> NotionConnectionStatus:
        connection = self._connection("notion")
        if connection is None:
            return NotionConnectionStatus(connected=False, status="disconnected")
        if self.secret_store is None or self.secret_store.implementation_id != connection.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(connection.secret_reference, namespace="notion")
        except SecretStoreError:
            raise IntegrationError(
                "connection_unavailable", "Operating-system secret storage could not remove the credential"
            ) from None
        try:
            self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Notion connection could not be removed safely") from None
        return NotionConnectionStatus(connected=False, status="disconnected")

    def remove_google_calendar_connection(self) -> GoogleCalendarConnectionStatus:
        connection = self._connection("google_calendar")
        if connection is None:
            return GoogleCalendarConnectionStatus(
                connected=False,
                status="disconnected",
                oauth_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            )
        if self.secret_store is None or self.secret_store.implementation_id != connection.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(
                connection.secret_reference,
                namespace=GOOGLE_CALENDAR_SECRET_NAMESPACE,
            )
        except SecretStoreError:
            raise IntegrationError(
                "connection_unavailable",
                "Operating-system secret storage could not remove the credential",
            ) from None
        try:
            self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Google Calendar connection could not be removed safely") from None
        return GoogleCalendarConnectionStatus(
            connected=False,
            status="disconnected",
            oauth_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
        )

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
            connection_available = all(
                self.operation_available(operation_id)
                for operation_id in requirement.operations
            )
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
                        "new operations or broader resource scope are approved",
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
                    "contract_fingerprint": self.contract_fingerprint(requirement),
                    "read_only": all(OPERATIONS[operation_id].read_only for operation_id in requirement.operations),
                "resource_scope": requirement.resource_scope.model_dump(mode="json"),
                "connection_available": all(
                    self.operation_available(operation_id)
                    for operation_id in requirement.operations
                ),
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

    def invoke_direct(
        self,
        operation_id: str,
        input_json: dict[str, Any],
        *,
        audit_record: Any | None = None,
    ) -> dict[str, Any]:
        """Invoke a provider operation as the trusted local user, without skill authorization."""

        return self._invoke_operation(
            operation_id,
            input_json,
            audit_record=audit_record,
            allowed_repositories=None,
        )

    def _invoke_checked(
        self,
        skill: Skill,
        caller: IntegrationCaller,
        operation_id: str,
        input_json: dict[str, Any],
        audit: IntegrationAuditRecord,
    ) -> dict[str, Any]:
        # The order is deliberate: GitHub credentials are retrieved only after every
        # caller, manifest, approval, connection, scope, and schema check passes.
        if skill.status != "installed" or not skill.enabled:
            raise IntegrationError("authorization_missing_or_stale", "Integration caller is not installed and enabled")
        if skill.runtime != caller.runtime or caller.runtime not in {"function", "web_app", "service"}:
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
        return self._invoke_operation(
            operation_id,
            input_json,
            audit_record=audit,
            allowed_repositories=set(requirement.resource_scope.repositories),
        )

    def _invoke_operation(
        self,
        operation_id: str,
        input_json: dict[str, Any],
        *,
        audit_record: Any | None,
        allowed_repositories: set[str] | None,
    ) -> dict[str, Any]:
        # Credentials are retrieved only after operation, connection, containment,
        # and input validation. Direct-user calls intentionally omit only the
        # skill-specific manifest and authorization checks above.
        operation = OPERATIONS.get(operation_id)
        if operation is None:
            raise IntegrationError("operation_undeclared", "Integration operation does not exist")
        connection = self._connection(operation.provider)
        if operation.provider == "atlas":
            if not self.provider_connected("atlas"):
                from app.services.atlas_settings_service import AtlasSettingsService

                status = AtlasSettingsService(self.db, secret_store=self.secret_store).status()
                if status.running and status.locked:
                    raise IntegrationError("atlas_locked", "Atlas is locked")
                raise IntegrationError("connection_unavailable", "Atlas is unavailable")
        elif connection is None or connection.status != "connected":
            provider_name = PROVIDER_DISPLAY_NAMES.get(operation.provider, operation.provider.title())
            raise IntegrationError("connection_unavailable", f"{provider_name} connection is unavailable")
        if not self.operation_available(operation_id):
            raise IntegrationError(
                "connection_unavailable",
                "The configured resource for this integration operation is unavailable",
            )
        scoped_resource = self._resource(operation.resource_scope, input_json)
        resource = scoped_resource
        if resource is None and "node_id" in operation.audit_resource_fields:
            node_id = input_json.get("node_id")
            if isinstance(node_id, int) and not isinstance(node_id, bool):
                resource = f"node:{node_id}"
        if resource is None and operation.provider == "notion" and "id" in operation.audit_resource_fields:
            page_id = input_json.get("id")
            if isinstance(page_id, str):
                resource = f"notion-page:{page_id}"
        if resource is None and operation.provider == "google_calendar" and "id" in operation.audit_resource_fields:
            event_id = input_json.get("id")
            if isinstance(event_id, str):
                resource = f"google-calendar-event:{event_id}"
        if audit_record is not None:
            audit_record.resource = resource
        if (
            allowed_repositories is not None
            and scoped_resource is not None
            and scoped_resource not in allowed_repositories
        ):
            raise IntegrationError("repository_outside_scope", "GitHub repository is outside the approved scope")
        try:
            Draft202012Validator(operation.input_schema).validate(input_json)
        except ValidationError as exc:
            path = ".".join(str(item) for item in exc.absolute_path)
            location = f" at {path}" if path else ""
            raise IntegrationError("invalid_input", f"Integration input is invalid{location}") from None
        credential = ""
        if operation.provider != "atlas":
            assert connection is not None
            if (
                self.secret_store is None
                or self.secret_store.implementation_id != connection.secret_store_id
            ):
                raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
            try:
                namespace = {
                    "notion": "notion",
                    "google_calendar": GOOGLE_CALENDAR_SECRET_NAMESPACE,
                }.get(operation.provider, "github")
                credential = self.secret_store.get(connection.secret_reference, namespace=namespace)
            except (SecretStoreError, RuntimeError):
                raise IntegrationError(
                    "connection_unavailable", "Stored integration credential is unavailable"
                ) from None
        try:
            try:
                if operation.operation_id == "atlas.knowledge.node.know":
                    inspected = self.atlas.execute(
                        OPERATIONS["atlas.knowledge.node.get"], {"node_id": input_json["node_id"]}
                    )
                    output = AtlasKnowledgeService(
                        self.atlas,
                        adapter=self.codex_adapter,
                        project_root=self.project_root,
                    ).know(inspected["node"], input_json.get("explanation"))
                elif operation.provider == "atlas":
                    output = self.atlas.execute(operation, input_json)
                elif operation.provider == "notion":
                    assert connection is not None
                    if operation.operation_id.startswith("notion.report."):
                        assert connection.configured_report_resource_id is not None
                        assert self.notion_report_provider_factory is not None
                        output = ReportService(
                            self.notion_report_provider_factory(
                                credential,
                                connection.configured_report_resource_id,
                            )
                        ).invoke(operation.operation_id, input_json)
                    else:
                        assert connection.configured_resource_id is not None
                        assert self.notion_provider_factory is not None
                        output = TodoService(
                            self.notion_provider_factory(credential, connection.configured_resource_id)
                        ).invoke(operation.operation_id, input_json)
                elif operation.provider == "google_calendar":
                    assert self.google_calendar is not None
                    output = self.google_calendar.execute(operation, input_json, credential)
                else:
                    output = self.github.execute(operation, input_json, credential)
            except IntegrationProviderError as exc:
                if (
                    operation.provider in {"github", "notion", "google_calendar"}
                    and exc.error_type == "invalid_credential"
                    and connection is not None
                ):
                    connection.status = "invalid"
                    connection.error_type = "invalid_credential"
                raise IntegrationError(
                    exc.error_type,
                    (
                        f"{provider_error_message(operation.provider, exc.error_type)}; retry after "
                        f"{exc.retry_after_seconds} seconds"
                        if exc.error_type == "rate_limited" and exc.retry_after_seconds is not None
                        else provider_error_message(operation.provider, exc.error_type)
                    ),
                    retry_after_seconds=exc.retry_after_seconds,
                ) from None
            except AtlasKnowledgeError as exc:
                raise IntegrationError(exc.error_type, str(exc)) from None
        finally:
            credential = ""
        try:
            Draft202012Validator(operation.output_schema).validate(output)
        except ValidationError:
            raise IntegrationError("internal_failure", "Integration returned an invalid normalized result") from None
        if (
            audit_record is not None
            and resource is None
            and operation.provider == "google_calendar"
            and isinstance(output.get("id"), str)
        ):
            audit_record.resource = f"google-calendar-event:{output['id']}"
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

    def _connected_notion_connection(self) -> IntegrationConnection:
        connection = self._connection("notion")
        if (
            connection is None
            or connection.status != "connected"
            or self.secret_store is None
            or self.secret_store.implementation_id != connection.secret_store_id
        ):
            raise IntegrationError("connection_unavailable", "Notion connection is unavailable")
        return connection

    def _validated_notion_identity(
        self,
        identity: dict[str, str | None],
    ) -> tuple[str, str, str | None]:
        bot_id = str(identity.get("bot_id") or "")
        if not bot_id or len(bot_id) > 128:
            raise IntegrationError("provider_unavailable", "Notion returned an invalid bot identity")
        bot_name = self._sanitized_identity(identity.get("bot_name"), 128) or "Notion bot"
        workspace_name = self._sanitized_identity(identity.get("workspace_name"), 256)
        return bot_id, bot_name, workspace_name

    @staticmethod
    def _notion_validation_error(
        target: str,
        error: IntegrationProviderError,
    ) -> IntegrationError:
        if error.error_type == "not_found":
            message = (
                f"The Notion {target} was not found. Copy its data source ID from Manage data sources "
                "and share the original database with this Notion connection."
            )
        elif error.error_type == "schema_mismatch":
            message = f"The Notion {target} does not match the required schema"
        elif error.error_type == "provider_forbidden":
            message = f"Notion denied access to the {target}"
        else:
            message = provider_error_message("notion", error.error_type)
        return IntegrationError(
            error.error_type,
            message,
            retry_after_seconds=error.retry_after_seconds,
        )

    def _connection(self, provider: str = "github") -> IntegrationConnection | None:
        return self.db.scalar(
            select(IntegrationConnection).where(IntegrationConnection.provider == provider)
        )

    def provider_connected(self, provider: str) -> bool:
        if provider == "atlas":
            try:
                from app.services.atlas_settings_service import AtlasSettingsService

                status = AtlasSettingsService(self.db, secret_store=self.secret_store).status()
                return bool(status.running and status.locked is False)
            except (AttributeError, ImportError, RuntimeError):
                return False
        connection = self._connection(provider)
        if connection is None or connection.status != "connected":
            return False
        if provider == "notion" and not connection.configured_resource_id:
            return False
        if self.secret_store is None or self.secret_store.implementation_id != connection.secret_store_id:
            return False
        return True

    def operation_available(self, operation_id: str) -> bool:
        operation = OPERATIONS.get(operation_id)
        if operation is None:
            return False
        if operation.provider != "notion":
            return self.provider_connected(operation.provider)
        connection = self._connection("notion")
        if (
            connection is None
            or connection.status != "connected"
            or self.secret_store is None
            or self.secret_store.implementation_id != connection.secret_store_id
        ):
            return False
        if operation_id.startswith("notion.report."):
            return bool(connection.configured_report_resource_id)
        return bool(connection.configured_resource_id)

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

    @staticmethod
    def _sanitized_identity(value: Any, max_length: int) -> str | None:
        if not isinstance(value, str):
            return None
        compact = " ".join(value.split())[:max_length]
        return compact or None


def build_default_integration_service(db: Session) -> IntegrationService:
    try:
        store = default_secret_store()
    except SecretStoreError:
        store = None
    return IntegrationService(db, secret_store=store)
