from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.integrations.authorization import IntegrationAuthorizationService
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.types import RiskLevel
from app.models import (
    ApprovalRequest,
    GoogleOAuthClientConfig,
    IntegrationAuthorization,
    IntegrationConnection,
    MicrosoftOAuthClientConfig,
    Skill,
)
from app.schemas.integration import (
    GitHubConnectionStatus,
    GmailConnectionStatus,
    GoogleCalendarConnectionStatus,
    GoogleOAuthClientStatus,
    MicrosoftOAuthClientStatus,
    NotionConnectionStatus,
    OutlookConnectionStatus,
)
from app.schemas.manifest import ManifestIntegrationRequirement, SkillManifest
from app.services.atlas_provider import AtlasProviderAdapter, UrllibAtlasProviderAdapter
from app.services.github_provider import (
    GitHubProviderAdapter,
    IntegrationProviderError,
    UrllibGitHubProviderAdapter,
)
from app.services.gmail_provider import (
    GMAIL_OAUTH_REDIRECT_URI,
    GMAIL_SECRET_NAMESPACE,
    GmailProviderAdapter,
    UrllibGmailProviderAdapter,
    gmail_oauth_state_store,
)
from app.services.google_calendar_provider import (
    GOOGLE_CALENDAR_SECRET_NAMESPACE,
    GOOGLE_OAUTH_REDIRECT_URI,
    GoogleCalendarProviderAdapter,
    UrllibGoogleCalendarProviderAdapter,
    google_oauth_state_store,
)
from app.services.google_oauth import (
    GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE,
    GoogleOAuthStateStore,
    parse_google_oauth_client,
    parse_google_oauth_credential,
    serialize_google_oauth_client,
)
from app.services.huggingface_provider import (
    HuggingFaceProviderAdapter,
    UrllibHuggingFaceProviderAdapter,
)
from app.services.notion_report_provider import NotionReportProvider
from app.services.notion_todo_provider import NotionTodoProvider
from app.services.outlook_provider import (
    OUTLOOK_AUTHORITY,
    OUTLOOK_CLIENT_SECRET_NAMESPACE,
    OUTLOOK_OAUTH_REDIRECT_URI,
    OUTLOOK_SECRET_NAMESPACE,
    OutlookProviderAdapter,
    outlook_oauth_state_store,
    parse_outlook_credential,
    serialize_outlook_credential,
)
from app.services.proposed_skill_service import ProposedSkillService
from app.services.report_service import ReportProvider
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store
from app.services.todo_service import TodoProvider


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
GMAIL_PROVIDER_ERROR_MESSAGES = {
    **GOOGLE_CALENDAR_PROVIDER_ERROR_MESSAGES,
    "not_found": "The requested Gmail message or conversation was not found",
    "provider_forbidden": "Google denied the requested Gmail operation",
    "rate_limited": "Gmail rate limited the integration request",
    "provider_timeout": "Gmail did not respond before the timeout",
    "response_too_large": "Gmail response exceeded the operation limit",
    "provider_unavailable": "Gmail is unavailable",
    "internal_failure": "Gmail integration failed safely",
}
OUTLOOK_PROVIDER_ERROR_MESSAGES = {
    "invalid_credential": "The Outlook authorization is invalid or revoked",
    "not_found": "The requested Outlook message or conversation was not found",
    "provider_forbidden": "Microsoft denied the requested Outlook operation",
    "rate_limited": "Outlook rate limited the integration request",
    "provider_timeout": "Outlook did not respond before the timeout",
    "response_too_large": "Outlook response exceeded the operation limit",
    "provider_unavailable": "Outlook is unavailable",
    "partial_mutation": "Outlook read state was only partially reconciled",
    "internal_failure": "Outlook integration failed safely",
}
TELEGRAM_PROVIDER_ERROR_MESSAGES = {
    **GOOGLE_CALENDAR_PROVIDER_ERROR_MESSAGES,
    "invalid_credential": "The Telegram bot token is invalid or revoked",
    "provider_forbidden": "Telegram denied the bot operation",
    "rate_limited": "Telegram rate limited the integration request",
    "provider_timeout": "Telegram did not respond before the timeout",
    "response_too_large": "Telegram response exceeded the operation limit",
    "provider_unavailable": "Telegram is unavailable",
    "internal_failure": "Telegram integration failed safely",
}
HUGGINGFACE_PROVIDER_ERROR_MESSAGES = {
    "not_found": "The requested Hugging Face paper was not found",
    "provider_forbidden": "The paper provider denied the requested operation",
    "rate_limited": "The paper provider rate limited the integration request",
    "provider_timeout": "The paper provider did not respond before the timeout",
    "response_too_large": "The paper provider response exceeded the operation limit",
    "unsupported_file_type": "The selected paper is not available as readable full text",
    "provider_unavailable": "Hugging Face or arXiv is unavailable",
    "internal_failure": "Hugging Face integration failed safely",
}
PROVIDER_DISPLAY_NAMES = {
    "github": "GitHub",
    "atlas": "Atlas",
    "notion": "Notion",
    "google_calendar": "Google Calendar",
    "gmail": "Gmail",
    "outlook": "Outlook",
    "huggingface": "Hugging Face",
}
GOOGLE_OAUTH_CLIENT_CONFIG_ID = 1
MICROSOFT_OAUTH_CLIENT_CONFIG_ID = 1
def provider_error_message(provider: str, error_type: str) -> str:
    if provider == "atlas":
        messages = ATLAS_PROVIDER_ERROR_MESSAGES
    elif provider == "notion":
        messages = NOTION_PROVIDER_ERROR_MESSAGES
    elif provider == "google_calendar":
        messages = GOOGLE_CALENDAR_PROVIDER_ERROR_MESSAGES
    elif provider == "gmail":
        messages = GMAIL_PROVIDER_ERROR_MESSAGES
    elif provider == "outlook":
        messages = OUTLOOK_PROVIDER_ERROR_MESSAGES
    elif provider == "telegram":
        messages = TELEGRAM_PROVIDER_ERROR_MESSAGES
    elif provider == "huggingface":
        messages = HUGGINGFACE_PROVIDER_ERROR_MESSAGES
    else:
        messages = PROVIDER_ERROR_MESSAGES
    return messages.get(error_type, messages["internal_failure"])


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IntegrationExecutionResult:
    output: dict[str, Any] | list[Any]
    audit_resource: str | None = None


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
    gmail: GmailProviderAdapter | None = None
    gmail_oauth_states: GoogleOAuthStateStore | None = None
    outlook: OutlookProviderAdapter | None = None
    outlook_oauth_states: Any | None = None
    huggingface: HuggingFaceProviderAdapter | None = None
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
        if self.gmail is None:
            self.gmail = UrllibGmailProviderAdapter()
        if self.gmail_oauth_states is None:
            self.gmail_oauth_states = gmail_oauth_state_store
        if self.outlook is None:
            self.outlook = OutlookProviderAdapter()
        if self.outlook_oauth_states is None:
            self.outlook_oauth_states = outlook_oauth_state_store
        if self.huggingface is None:
            self.huggingface = UrllibHuggingFaceProviderAdapter()

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
                    is_default=True,
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                    status="connected",
                    account_login=identity["login"],
                    account_id=identity["id"],
                    created_at=now,
                    updated_at=now,
                    last_validated_at=now,
                )
                self._make_default("github", connection)
                self.db.add(connection)
            else:
                connection = previous
                self._make_default("github", connection)
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
        google_client = self.google_oauth_client_status()
        available = (
            google_client.configured
            and self.secret_store is not None
            and self.secret_store.implementation_id == connection.secret_store_id
        )
        status = connection.status if available else "unavailable"
        return GoogleCalendarConnectionStatus(
            connected=available and connection.status == "connected",
            status=status,
            account_email=connection.account_login,
            last_validated_at=connection.last_validated_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            error_type=(connection.error_type or google_client.error_type) if status != "connected" else None,
            oauth_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
        )

    def google_oauth_client_status(self) -> GoogleOAuthClientStatus:
        try:
            config = self._google_oauth_client_config(migrate_legacy=True)
        except IntegrationError as exc:
            return GoogleOAuthClientStatus(
                configured=False,
                status="conflict" if exc.error_type == "google_oauth_configuration_conflict" else "unavailable",
                calendar_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
                gmail_redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
                error_type=exc.error_type,
            )
        if config is None:
            return GoogleOAuthClientStatus(
                configured=False,
                status="not_configured",
                calendar_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
                gmail_redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
            )
        available = self.secret_store is not None and self.secret_store.implementation_id == config.secret_store_id
        return GoogleOAuthClientStatus(
            configured=available,
            status="configured" if available else "unavailable",
            calendar_redirect_uri=GOOGLE_OAUTH_REDIRECT_URI,
            gmail_redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
            created_at=config.created_at,
            updated_at=config.updated_at,
            error_type=None if available else "connection_unavailable",
        )

    def configure_google_oauth_client(self, client_id: str, client_secret: str) -> GoogleOAuthClientStatus:
        if not 1 <= len(client_id) <= 1024 or not 1 <= len(client_secret) <= 4096:
            raise IntegrationError("invalid_input", "Google OAuth client credentials must be non-empty bounded strings")
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        existing = self._google_oauth_client_config(migrate_legacy=True)
        existing_client: dict[str, str] | None = None
        if existing is not None:
            if self.secret_store.implementation_id != existing.secret_store_id:
                raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
            try:
                existing_client = parse_google_oauth_client(
                    self.secret_store.get(
                        existing.secret_reference,
                        namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE,
                    )
                )
            except (IntegrationProviderError, SecretStoreError):
                raise IntegrationError("connection_unavailable", "Stored Google OAuth client is unavailable") from None
            if existing_client == {"client_id": client_id, "client_secret": client_secret}:
                return self.google_oauth_client_status()
            if self._connection("google_calendar") is not None or self._connection("gmail") is not None:
                raise IntegrationError(
                    "google_oauth_configuration_in_use",
                    "Disconnect Calendar and Gmail before replacing the shared Google OAuth client",
                )
        serialized = serialize_google_oauth_client(client_id, client_secret)
        try:
            new_reference = self.secret_store.put(
                serialized,
                namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE,
            )
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            serialized = ""
        previous_reference = existing.secret_reference if existing is not None else None
        now = utc_now()
        try:
            if existing is None:
                config = GoogleOAuthClientConfig(
                    id=GOOGLE_OAUTH_CLIENT_CONFIG_ID,
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(config)
            else:
                existing.secret_store_id = self.secret_store.implementation_id
                existing.secret_reference = new_reference
                existing.updated_at = now
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Google OAuth client could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference, namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        return self.google_oauth_client_status()

    def remove_google_oauth_client(self) -> GoogleOAuthClientStatus:
        config = self._google_oauth_client_config(migrate_legacy=False)
        if config is None:
            return self.google_oauth_client_status()
        if self._connection("google_calendar") is not None or self._connection("gmail") is not None:
            raise IntegrationError(
                "google_oauth_configuration_in_use",
                "Disconnect Calendar and Gmail before removing the shared Google OAuth client",
            )
        if self.secret_store is None or self.secret_store.implementation_id != config.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(config.secret_reference, namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE)
            self.db.delete(config)
            self.db.commit()
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Google OAuth client could not be removed") from None
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Google OAuth client could not be removed safely") from None
        return self.google_oauth_client_status()

    def start_google_calendar_oauth(self) -> str:
        client = self._required_google_oauth_client()
        assert self.google_oauth_states is not None
        assert self.google_calendar is not None
        state = self.google_oauth_states.create(client["client_id"], client["client_secret"])
        return self.google_calendar.authorization_url(client["client_id"], state)

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
            {"refresh_token": tokens["refresh_token"]},
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
                    is_default=True,
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
                self._make_default("google_calendar", connection)
                self.db.add(connection)
            else:
                connection = previous
                self._make_default("google_calendar", connection)
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

    def gmail_connection_status(self) -> GmailConnectionStatus:
        connection = self._connection("gmail")
        if connection is None:
            return GmailConnectionStatus(
                connected=False,
                status="disconnected",
                oauth_redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
            )
        google_client = self.google_oauth_client_status()
        available = (
            google_client.configured
            and self.secret_store is not None
            and self.secret_store.implementation_id == connection.secret_store_id
        )
        status = connection.status if available else "unavailable"
        return GmailConnectionStatus(
            connected=available and connection.status == "connected",
            status=status,
            account_email=connection.account_login,
            last_validated_at=connection.last_validated_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            error_type=(connection.error_type or google_client.error_type) if status != "connected" else None,
            oauth_redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
        )

    def start_gmail_oauth(self) -> str:
        client = self._required_google_oauth_client()
        assert self.gmail_oauth_states is not None
        assert self.gmail is not None
        state = self.gmail_oauth_states.create(client["client_id"], client["client_secret"])
        return self.gmail.authorization_url(client["client_id"], state)

    def discard_gmail_oauth(self, state: str) -> None:
        if not 1 <= len(state) <= 512:
            raise IntegrationError("invalid_credential", "Gmail OAuth response is invalid or expired")
        assert self.gmail_oauth_states is not None
        try:
            self.gmail_oauth_states.consume(state)
        except IntegrationProviderError as exc:
            raise IntegrationError(exc.error_type, provider_error_message("gmail", exc.error_type)) from None

    def complete_gmail_oauth(self, state: str, code: str) -> GmailConnectionStatus:
        if not 1 <= len(state) <= 512 or not 1 <= len(code) <= 8192:
            raise IntegrationError("invalid_credential", "Gmail OAuth response is invalid or expired")
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        assert self.gmail_oauth_states is not None
        assert self.gmail is not None
        try:
            pending = self.gmail_oauth_states.consume(state)
            tokens = self.gmail.exchange_code(pending, code)
            identity = self.gmail.identity(tokens["access_token"])
        except IntegrationProviderError as exc:
            raise IntegrationError(exc.error_type, provider_error_message("gmail", exc.error_type)) from None
        credential = json.dumps(
            {"refresh_token": tokens["refresh_token"]},
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            new_reference = self.secret_store.put(credential, namespace=GMAIL_SECRET_NAMESPACE)
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            credential = ""
            tokens = {}
        previous = self._connection("gmail")
        previous_reference = previous.secret_reference if previous is not None else None
        previous_account_id = previous.account_id if previous is not None else None
        now = utc_now()
        try:
            if previous is None:
                connection = IntegrationConnection(
                    provider="gmail",
                    is_default=True,
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
                self._make_default("gmail", connection)
                self.db.add(connection)
            else:
                connection = previous
                self._make_default("gmail", connection)
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
                self.invalidate_provider_authorizations("gmail", "Gmail account identity changed")
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace=GMAIL_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Gmail connection could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference, namespace=GMAIL_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        return self.gmail_connection_status()

    def microsoft_oauth_client_status(self) -> MicrosoftOAuthClientStatus:
        config = self._microsoft_oauth_client_config()
        if config is None:
            return MicrosoftOAuthClientStatus(
                configured=False,
                status="not_configured",
                authority=OUTLOOK_AUTHORITY,
                outlook_redirect_uri=OUTLOOK_OAUTH_REDIRECT_URI,
            )
        available = self.secret_store is not None and self.secret_store.implementation_id == config.secret_store_id
        return MicrosoftOAuthClientStatus(
            configured=available,
            status="configured" if available else "unavailable",
            authority=config.authority,
            outlook_redirect_uri=OUTLOOK_OAUTH_REDIRECT_URI,
            created_at=config.created_at,
            updated_at=config.updated_at,
            error_type=None if available else "connection_unavailable",
        )

    def configure_microsoft_oauth_client(self, client_id: str, client_secret: str) -> MicrosoftOAuthClientStatus:
        if not 1 <= len(client_id) <= 1024 or not 1 <= len(client_secret) <= 4096:
            raise IntegrationError("invalid_input", "Microsoft OAuth client credentials must be non-empty bounded strings")
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        existing = self._microsoft_oauth_client_config()
        if existing is not None and self._connection("outlook") is not None:
            if self.secret_store.implementation_id != existing.secret_store_id:
                raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
            try:
                previous_secret = self.secret_store.get(
                    existing.secret_reference,
                    namespace=OUTLOOK_CLIENT_SECRET_NAMESPACE,
                )
            except SecretStoreError:
                previous_secret = ""
            if previous_secret == client_secret and existing.client_id == client_id:
                return self.microsoft_oauth_client_status()
            raise IntegrationError(
                "microsoft_oauth_configuration_in_use",
                "Disconnect Outlook before replacing the Microsoft OAuth client",
            )
        try:
            new_reference = self.secret_store.put(client_secret, namespace=OUTLOOK_CLIENT_SECRET_NAMESPACE)
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        now = utc_now()
        previous_reference = existing.secret_reference if existing is not None else None
        try:
            if existing is None:
                self.db.add(
                    MicrosoftOAuthClientConfig(
                        id=MICROSOFT_OAUTH_CLIENT_CONFIG_ID,
                        client_id=client_id,
                        secret_store_id=self.secret_store.implementation_id,
                        secret_reference=new_reference,
                        authority=OUTLOOK_AUTHORITY,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.client_id = client_id
                existing.secret_store_id = self.secret_store.implementation_id
                existing.secret_reference = new_reference
                existing.authority = OUTLOOK_AUTHORITY
                existing.updated_at = now
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace=OUTLOOK_CLIENT_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Microsoft OAuth client could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference, namespace=OUTLOOK_CLIENT_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        return self.microsoft_oauth_client_status()

    def remove_microsoft_oauth_client(self) -> MicrosoftOAuthClientStatus:
        config = self._microsoft_oauth_client_config()
        if config is None:
            return self.microsoft_oauth_client_status()
        if self._connection("outlook") is not None:
            raise IntegrationError(
                "microsoft_oauth_configuration_in_use",
                "Disconnect Outlook before removing the Microsoft OAuth client",
            )
        if self.secret_store is None or self.secret_store.implementation_id != config.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(config.secret_reference, namespace=OUTLOOK_CLIENT_SECRET_NAMESPACE)
            self.db.delete(config)
            self.db.commit()
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage could not remove the client") from None
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Microsoft OAuth client could not be removed safely") from None
        return self.microsoft_oauth_client_status()

    def outlook_connection_status(self) -> OutlookConnectionStatus:
        connection = self._connection("outlook")
        if connection is None:
            return OutlookConnectionStatus(
                connected=False,
                status="disconnected",
                oauth_redirect_uri=OUTLOOK_OAUTH_REDIRECT_URI,
            )
        client = self.microsoft_oauth_client_status()
        available = client.configured and self.secret_store is not None and self.secret_store.implementation_id == connection.secret_store_id
        status = connection.status if available else "unavailable"
        return OutlookConnectionStatus(
            connected=available and connection.status == "connected",
            status=status,
            account_email=connection.account_login,
            account_id=connection.account_id,
            last_validated_at=connection.last_validated_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            error_type=(connection.error_type or client.error_type) if status != "connected" else None,
            oauth_redirect_uri=OUTLOOK_OAUTH_REDIRECT_URI,
        )

    def start_outlook_oauth(self) -> str:
        client = self._required_microsoft_oauth_client()
        assert self.outlook is not None
        assert self.outlook_oauth_states is not None
        _state, url = self.outlook.begin_authorization(
            client["client_id"],
            client["client_secret"],
            self.outlook_oauth_states,
        )
        return url

    def discard_outlook_oauth(self, state: str) -> None:
        if not 1 <= len(state) <= 512:
            raise IntegrationError("invalid_credential", "Microsoft OAuth response is invalid or expired")
        assert self.outlook_oauth_states is not None
        try:
            self.outlook_oauth_states.consume(state)
        except IntegrationProviderError as exc:
            raise IntegrationError(exc.error_type, provider_error_message("outlook", exc.error_type)) from None

    def complete_outlook_oauth(self, state: str, code: str) -> OutlookConnectionStatus:
        if not 1 <= len(state) <= 512 or not 1 <= len(code) <= 8192:
            raise IntegrationError("invalid_credential", "Microsoft OAuth response is invalid or expired")
        if self.secret_store is None:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        assert self.outlook_oauth_states is not None
        assert self.outlook is not None
        try:
            pending = self.outlook_oauth_states.consume(state)
            tokens = self.outlook.exchange_code(pending, code)
            identity = self.outlook.identity(tokens["access_token"])
        except IntegrationProviderError as exc:
            raise IntegrationError(exc.error_type, provider_error_message("outlook", exc.error_type)) from None
        credential = serialize_outlook_credential(tokens["refresh_token"])
        try:
            new_reference = self.secret_store.put(credential, namespace=OUTLOOK_SECRET_NAMESPACE)
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            credential = ""
            tokens = {}
        previous = self._connection("outlook")
        previous_reference = previous.secret_reference if previous is not None else None
        previous_account_id = previous.account_id if previous is not None else None
        now = utc_now()
        try:
            if previous is None:
                connection = IntegrationConnection(
                    provider="outlook",
                    is_default=True,
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
                self._make_default("outlook", connection)
                self.db.add(connection)
            else:
                previous.is_default = True
                previous.secret_store_id = self.secret_store.implementation_id
                previous.secret_reference = new_reference
                previous.credential_kind = "oauth_refresh"
                previous.status = "connected"
                previous.account_login = identity["email"]
                previous.account_id = identity["account_id"]
                previous.error_type = None
                previous.updated_at = now
                previous.last_validated_at = now
            if previous_account_id is not None and previous_account_id != identity["account_id"]:
                self.invalidate_provider_authorizations("outlook", "Outlook account identity changed")
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace=OUTLOOK_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Outlook connection could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            try:
                self.secret_store.delete(previous_reference, namespace=OUTLOOK_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        return self.outlook_connection_status()

    def remove_outlook_connection(self) -> OutlookConnectionStatus:
        connection = self._connection("outlook")
        if connection is None:
            return OutlookConnectionStatus(connected=False, status="disconnected", oauth_redirect_uri=OUTLOOK_OAUTH_REDIRECT_URI)
        if self.secret_store is None or self.secret_store.implementation_id != connection.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(connection.secret_reference, namespace=OUTLOOK_SECRET_NAMESPACE)
            self.invalidate_provider_authorizations("outlook", "Outlook connection removed")
            self.db.delete(connection)
            self.db.commit()
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage could not remove the credential") from None
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Outlook connection could not be removed safely") from None
        return OutlookConnectionStatus(connected=False, status="disconnected", oauth_redirect_uri=OUTLOOK_OAUTH_REDIRECT_URI)

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
                    is_default=True,
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
                self._make_default("notion", connection)
                self.db.add(connection)
            else:
                self._make_default("notion", previous)
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
                    is_default=True,
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
                self._make_default("notion", connection)
                self.db.add(connection)
            else:
                connection = previous
                self._make_default("notion", connection)
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

    def remove_gmail_connection(self) -> GmailConnectionStatus:
        connection = self._connection("gmail")
        if connection is None:
            return GmailConnectionStatus(
                connected=False,
                status="disconnected",
                oauth_redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
            )
        if self.secret_store is None or self.secret_store.implementation_id != connection.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            self.secret_store.delete(connection.secret_reference, namespace=GMAIL_SECRET_NAMESPACE)
        except SecretStoreError:
            raise IntegrationError(
                "connection_unavailable",
                "Operating-system secret storage could not remove the credential",
            ) from None
        try:
            self.invalidate_provider_authorizations("gmail", "Gmail connection removed")
            self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise IntegrationError("internal_failure", "Gmail connection could not be removed safely") from None
        return GmailConnectionStatus(
            connected=False,
            status="disconnected",
            oauth_redirect_uri=GMAIL_OAUTH_REDIRECT_URI,
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
            current = IntegrationAuthorizationService(
                self.db,
                DEFAULT_INTEGRATION_REGISTRY,
            ).authorization(skill, requirement)
            if current is not None:
                if current.approval_request.status in {"pending", "approved"}:
                    authorizations.append(current)
                    continue
                self._invalidate_authorization(current, "A new decision was requested for this integration contract")
            connection_available = all(
                self.operation_available(operation_id)
                for operation_id in requirement.operations
            )
            operations = [
                DEFAULT_INTEGRATION_REGISTRY.operation_mapping[operation_id]
                for operation_id in requirement.operations
            ]
            repositories = list(requirement.resource_scope.repositories)
            read_only = all(operation.read_only for operation in operations)
            provider_name = PROVIDER_DISPLAY_NAMES.get(requirement.provider, requirement.provider.title())
            action_description = "read-only access" if read_only else "read and bounded write access"
            mutation_detail = (
                " The Knowledge write uses one internet-enabled Codex call, writes one selected node, and may create "
                "immediate unassessed children through primitive Atlas operations; it cannot rename, move, delete, "
                "merge, or recursively expand nodes. The node update and each child creation are separately atomic."
                if any(operation.id == "atlas.knowledge.node.know" for operation in operations)
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
                risk_level=(
                    "high"
                    if any(operation.risk is RiskLevel.HIGH for operation in operations)
                    else "medium"
                    if any(operation.risk is RiskLevel.MEDIUM for operation in operations)
                    else "low"
                ),
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
        return IntegrationAuthorizationService(
            self.db,
            DEFAULT_INTEGRATION_REGISTRY,
        ).authorization_state(skill, requirement)

    def integration_review(self, skill: Skill, manifest: SkillManifest) -> list[dict[str, Any]]:
        return [
                {
                    "provider": requirement.provider,
                    "operations": list(requirement.operations),
                    "contract_fingerprint": self.contract_fingerprint(requirement),
                    "read_only": all(
                        DEFAULT_INTEGRATION_REGISTRY.operation_mapping[operation_id].read_only
                        for operation_id in requirement.operations
                    ),
                "resource_scope": requirement.resource_scope.model_dump(mode="json"),
                "connection_available": all(
                    self.operation_available(operation_id)
                    for operation_id in requirement.operations
                ),
                "authorization_state": self.authorization_state(skill, requirement),
            }
            for requirement in manifest.integration_requirements
        ]

    def execute_context(
        self,
        context: InvocationContext,
        operation_id: str,
        input_json: dict[str, Any],
    ) -> IntegrationExecutionResult:
        """Compatibility façade for callers not yet constructed through the handler."""
        from app.integrations.invocation import (
            IntegrationInvocationError,
            IntegrationInvocationService,
        )

        try:
            result = IntegrationInvocationService(
                self.db,
                compatibility_service=self,
                project_root=self.project_root,
            ).execute(context, operation_id, input_json)
        except IntegrationInvocationError as exc:
            raise IntegrationError(exc.error_type, str(exc)) from None
        return IntegrationExecutionResult(
            output=result.output,
            audit_resource=result.audit_resource,
        )

    def execute_claimed_approval(
        self,
        approval,
        context: InvocationContext,
    ) -> IntegrationExecutionResult:
        """Compatibility façade; approved execution still re-enters policy."""
        from app.integrations.invocation import (
            IntegrationInvocationError,
            IntegrationInvocationService,
        )

        try:
            result = IntegrationInvocationService(
                self.db,
                compatibility_service=self,
                project_root=self.project_root,
            ).execute_approved(approval, context)
        except IntegrationInvocationError as exc:
            raise IntegrationError(exc.error_type, str(exc)) from None
        return IntegrationExecutionResult(
            output=result.output,
            audit_resource=result.audit_resource,
        )

    def contract_fingerprint(self, requirement: ManifestIntegrationRequirement) -> str:
        return IntegrationAuthorizationService(
            self.db,
            DEFAULT_INTEGRATION_REGISTRY,
        ).contract_fingerprint(requirement)

    def operation_contract_fingerprint(self, operation_id: str) -> str:
        operation = DEFAULT_INTEGRATION_REGISTRY.get(operation_id)
        if operation is None:
            raise IntegrationError("operation_undeclared", "Integration operation does not exist")
        from app.integrations.policy import IntegrationCapabilityPolicy

        return IntegrationCapabilityPolicy(
            self.db,
            registry=DEFAULT_INTEGRATION_REGISTRY,
            connection_service=self,
            project_root=self.project_root,
        ).operation_contract_fingerprint(operation)

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

    def _google_oauth_client_config(self, *, migrate_legacy: bool) -> GoogleOAuthClientConfig | None:
        config = self.db.get(GoogleOAuthClientConfig, GOOGLE_OAUTH_CLIENT_CONFIG_ID)
        if config is None and migrate_legacy:
            config = self._migrate_legacy_google_oauth_client()
        return config

    def _migrate_legacy_google_oauth_client(self) -> GoogleOAuthClientConfig | None:
        if self.secret_store is None:
            return None
        clients: dict[tuple[str, str], dict[str, str]] = {}
        legacy_connections: list[
            tuple[IntegrationConnection, str, str, dict[str, str]]
        ] = []
        for provider, namespace in (
            ("google_calendar", GOOGLE_CALENDAR_SECRET_NAMESPACE),
            ("gmail", GMAIL_SECRET_NAMESPACE),
        ):
            connection = self._connection(provider)
            if connection is None or connection.secret_store_id != self.secret_store.implementation_id:
                continue
            try:
                legacy = parse_google_oauth_credential(
                    self.secret_store.get(connection.secret_reference, namespace=namespace)
                )
            except (IntegrationProviderError, SecretStoreError):
                continue
            key = (legacy["client_id"], legacy["client_secret"])
            clients[key] = {"client_id": key[0], "client_secret": key[1]}
            legacy_connections.append(
                (connection, namespace, connection.secret_reference, legacy)
            )
        if not clients:
            return None
        if len(clients) != 1:
            raise IntegrationError(
                "google_oauth_configuration_conflict",
                "Existing Calendar and Gmail connections use different OAuth clients; disconnect both and configure one shared client",
            )
        client = next(iter(clients.values()))
        serialized = serialize_google_oauth_client(client["client_id"], client["client_secret"])
        service_replacements: list[tuple[IntegrationConnection, str, str, str]] = []
        try:
            reference = self.secret_store.put(
                serialized,
                namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE,
            )
            for connection, namespace, previous_reference, legacy in legacy_connections:
                service_credential = json.dumps(
                    {"refresh_token": legacy["refresh_token"]},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                new_reference = self.secret_store.put(
                    service_credential,
                    namespace=namespace,
                )
                service_replacements.append(
                    (connection, namespace, previous_reference, new_reference)
                )
        except SecretStoreError:
            for _connection, namespace, _previous_reference, new_reference in service_replacements:
                try:
                    self.secret_store.delete(new_reference, namespace=namespace)
                except SecretStoreError:
                    pass
            if "reference" in locals():
                try:
                    self.secret_store.delete(
                        reference,
                        namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE,
                    )
                except SecretStoreError:
                    pass
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            serialized = ""
            client = {}
        now = utc_now()
        config = GoogleOAuthClientConfig(
            id=GOOGLE_OAUTH_CLIENT_CONFIG_ID,
            secret_store_id=self.secret_store.implementation_id,
            secret_reference=reference,
            created_at=now,
            updated_at=now,
        )
        try:
            self.db.add(config)
            for connection, _namespace, _previous_reference, new_reference in service_replacements:
                connection.secret_reference = new_reference
                connection.updated_at = now
            self.db.commit()
            self.db.refresh(config)
        except Exception:
            self.db.rollback()
            for _connection, namespace, _previous_reference, new_reference in service_replacements:
                try:
                    self.secret_store.delete(new_reference, namespace=namespace)
                except SecretStoreError:
                    pass
            try:
                self.secret_store.delete(reference, namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise IntegrationError("internal_failure", "Google OAuth client migration failed safely") from None
        for _connection, namespace, previous_reference, _new_reference in service_replacements:
            try:
                self.secret_store.delete(previous_reference, namespace=namespace)
            except SecretStoreError:
                pass
        return config

    def _required_google_oauth_client(self) -> dict[str, str]:
        config = self._google_oauth_client_config(migrate_legacy=True)
        if config is None:
            raise IntegrationError("google_oauth_not_configured", "Configure the shared Google OAuth client first")
        if self.secret_store is None or self.secret_store.implementation_id != config.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            return parse_google_oauth_client(
                self.secret_store.get(
                    config.secret_reference,
                    namespace=GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE,
                )
            )
        except (IntegrationProviderError, SecretStoreError):
            raise IntegrationError("connection_unavailable", "Stored Google OAuth client is unavailable") from None

    def _google_runtime_credential(self, service_credential: str) -> str:
        try:
            value = json.loads(service_credential)
        except (json.JSONDecodeError, TypeError):
            raise IntegrationError("invalid_credential", "Stored Google authorization is invalid") from None
        if not isinstance(value, dict) or not isinstance(value.get("refresh_token"), str) or not value["refresh_token"]:
            raise IntegrationError("invalid_credential", "Stored Google authorization is invalid")
        client = self._required_google_oauth_client()
        return json.dumps(
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": value["refresh_token"],
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _microsoft_oauth_client_config(self) -> MicrosoftOAuthClientConfig | None:
        return self.db.get(MicrosoftOAuthClientConfig, MICROSOFT_OAUTH_CLIENT_CONFIG_ID)

    def _required_microsoft_oauth_client(self) -> dict[str, str]:
        config = self._microsoft_oauth_client_config()
        if config is None:
            raise IntegrationError("microsoft_oauth_not_configured", "Configure the Microsoft OAuth client first")
        if self.secret_store is None or self.secret_store.implementation_id != config.secret_store_id:
            raise IntegrationError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            secret = self.secret_store.get(config.secret_reference, namespace=OUTLOOK_CLIENT_SECRET_NAMESPACE)
        except SecretStoreError:
            raise IntegrationError("connection_unavailable", "Stored Microsoft OAuth client is unavailable") from None
        if not isinstance(secret, str) or not secret:
            raise IntegrationError("connection_unavailable", "Stored Microsoft OAuth client is unavailable")
        return {"client_id": config.client_id, "client_secret": secret}

    def _outlook_runtime_credential(self, service_credential: str) -> str:
        try:
            refresh = parse_outlook_credential(service_credential)["refresh_token"]
            client = self._required_microsoft_oauth_client()
        except (IntegrationProviderError, IntegrationError) as exc:
            error_type = getattr(exc, "error_type", "connection_unavailable")
            raise IntegrationError(str(error_type), "Stored Microsoft authorization is unavailable") from None
        return json.dumps(
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": refresh,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _make_default(self, provider: str, connection: IntegrationConnection) -> None:
        self.db.query(IntegrationConnection).filter(
            IntegrationConnection.provider == provider,
            IntegrationConnection.is_default.is_(True),
            IntegrationConnection.id != connection.id,
        ).update({IntegrationConnection.is_default: False}, synchronize_session=False)
        connection.is_default = True

    def _connection(self, provider: str = "github") -> IntegrationConnection | None:
        return self.db.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.provider == provider)
            .where(IntegrationConnection.is_default.is_(True))
            .order_by(IntegrationConnection.id.desc())
        )

    def provider_connected(self, provider: str) -> bool:
        if provider == "huggingface":
            return True
        if provider == "atlas":
            try:
                from app.services.atlas_settings_service import AtlasSettingsService

                status = AtlasSettingsService(self.db, secret_store=self.secret_store).status()
                return bool(status.running and status.locked is False)
            except (AttributeError, ImportError, RuntimeError):
                return False
        if provider == "telegram":
            try:
                from app.services.telegram_service import TelegramService

                return TelegramService(
                    self.db,
                    secret_store=self.secret_store,
                ).approval_available()
            except (AttributeError, ImportError, RuntimeError):
                return False
        connection = self._connection(provider)
        if connection is None or connection.status != "connected":
            return False
        if provider == "notion" and not connection.configured_resource_id:
            return False
        if self.secret_store is None or self.secret_store.implementation_id != connection.secret_store_id:
            return False
        if provider in {"google_calendar", "gmail"}:
            try:
                config = self._google_oauth_client_config(migrate_legacy=True)
            except IntegrationError:
                return False
            if (
                config is None
                or self.secret_store is None
                or config.secret_store_id != self.secret_store.implementation_id
            ):
                return False
        if provider == "outlook":
            config = self._microsoft_oauth_client_config()
            if (
                config is None
                or self.secret_store is None
                or config.secret_store_id != self.secret_store.implementation_id
            ):
                return False
        return True

    def operation_available(self, operation_id: str) -> bool:
        operation = DEFAULT_INTEGRATION_REGISTRY.get(operation_id)
        if operation is None:
            return False
        providers = DEFAULT_INTEGRATION_REGISTRY.operation_provider_set(operation_id)
        if not providers:
            return False
        if operation_id.startswith("notion."):
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
        return any(self.provider_connected(provider) for provider in providers)

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
