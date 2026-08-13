from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IntegrationAuthorization, IntegrationConnection
from app.schemas.atlas_settings import AtlasSettingsStatus
from app.services.atlas_lifecycle_service import (
    AtlasLifecycleError,
    AtlasLifecycleService,
    atlas_lifecycle_service,
)
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store

ATLAS_KEY_NAMESPACE = "atlas_api_key"
ATLAS_PASSPHRASE_NAMESPACE = "atlas_passphrase"


class AtlasSettingsError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class AtlasSettingsService:
    db: Session
    lifecycle: AtlasLifecycleService = atlas_lifecycle_service
    secret_store: SecretStore | None = None

    def status(self) -> AtlasSettingsStatus:
        runtime = self.lifecycle.status()
        connection = self._connection()
        key_status = "missing"
        error_type = None
        if connection is not None:
            if not self._store_matches(connection.secret_store_id):
                key_status = "unavailable"
            elif runtime["running"]:
                try:
                    self.lifecycle.validate_api_key(self.api_key())
                    key_status = "connected"
                except (AtlasLifecycleError, AtlasSettingsError) as exc:
                    key_status = "invalid" if exc.error_type == "invalid_api_key" else "unavailable"
                    error_type = exc.error_type
            else:
                key_status = "unavailable"
        return AtlasSettingsStatus(
            directory=str(self.lifecycle.directory),
            running=runtime["running"],
            process_ownership=self.lifecycle.ownership,
            initialized=runtime["initialized"],
            locked=runtime["locked"],
            api_key_status=key_status,
            auto_unlock_configured=self._passphrase_configured(connection),
            startup_error=self.lifecycle.startup_error,
            error_type=error_type,
        )

    def put_api_key(self, api_key: str) -> AtlasSettingsStatus:
        self._bounded_secret(api_key, "Atlas API key")
        store = self._require_store()
        try:
            self.lifecycle.validate_api_key(api_key)
        except AtlasLifecycleError as exc:
            raise AtlasSettingsError(exc.error_type, str(exc)) from None
        try:
            new_reference = store.put(api_key, namespace=ATLAS_KEY_NAMESPACE)
        except SecretStoreError:
            raise AtlasSettingsError("secret_store_unavailable", "Operating-system secret storage is unavailable") from None
        previous = self._connection()
        previous_reference = previous.secret_reference if previous else None
        now = utc_now()
        try:
            if previous is None:
                connection = IntegrationConnection(
                    id=2,
                    provider="atlas",
                    secret_store_id=store.implementation_id,
                    secret_reference=new_reference,
                    status="connected",
                    account_login="Local Atlas",
                    account_id="local-atlas",
                    created_at=now,
                    updated_at=now,
                    last_validated_at=now,
                )
                self.db.add(connection)
            else:
                previous.secret_store_id = store.implementation_id
                previous.secret_reference = new_reference
                previous.status = "connected"
                previous.error_type = None
                previous.updated_at = now
                previous.last_validated_at = now
            self.db.commit()
        except Exception:
            self.db.rollback()
            self._best_effort_delete(new_reference, ATLAS_KEY_NAMESPACE)
            raise AtlasSettingsError("internal_failure", "Atlas API key could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            self._best_effort_delete(previous_reference, ATLAS_KEY_NAMESPACE)
        return self.status()

    def remove_connection(self) -> AtlasSettingsStatus:
        connection = self._connection()
        if connection is None:
            return self.status()
        key_reference = connection.secret_reference
        passphrase_reference = connection.passphrase_secret_reference
        self._invalidate_authorizations("Atlas connection removed")
        try:
            self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise AtlasSettingsError("internal_failure", "Atlas connection could not be removed safely") from None
        self._best_effort_delete(key_reference, ATLAS_KEY_NAMESPACE)
        if passphrase_reference:
            self._best_effort_delete(passphrase_reference, ATLAS_PASSPHRASE_NAMESPACE)
        return self.status()

    def put_passphrase(self, passphrase: str) -> AtlasSettingsStatus:
        self._bounded_secret(passphrase, "Atlas passphrase")
        connection = self._required_connection()
        store = self._require_store(connection.secret_store_id)
        self._validate_before_unlock(connection)
        try:
            self.lifecycle.unlock(passphrase)
        except AtlasLifecycleError as exc:
            raise AtlasSettingsError(exc.error_type, str(exc)) from None
        try:
            new_reference = store.put(passphrase, namespace=ATLAS_PASSPHRASE_NAMESPACE)
        except SecretStoreError:
            raise AtlasSettingsError("secret_store_unavailable", "Operating-system secret storage is unavailable") from None
        previous_reference = connection.passphrase_secret_reference
        try:
            connection.passphrase_secret_store_id = store.implementation_id
            connection.passphrase_secret_reference = new_reference
            connection.updated_at = utc_now()
            self.db.commit()
        except Exception:
            self.db.rollback()
            self._best_effort_delete(new_reference, ATLAS_PASSPHRASE_NAMESPACE)
            raise AtlasSettingsError("internal_failure", "Atlas passphrase could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            self._best_effort_delete(previous_reference, ATLAS_PASSPHRASE_NAMESPACE)
        return self.status()

    def remove_passphrase(self) -> AtlasSettingsStatus:
        connection = self._connection()
        if connection is None or connection.passphrase_secret_reference is None:
            return self.status()
        reference = connection.passphrase_secret_reference
        connection.passphrase_secret_store_id = None
        connection.passphrase_secret_reference = None
        connection.updated_at = utc_now()
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise AtlasSettingsError("internal_failure", "Atlas passphrase could not be removed safely") from None
        self._best_effort_delete(reference, ATLAS_PASSPHRASE_NAMESPACE)
        return self.status()

    def unlock_now(self) -> AtlasSettingsStatus:
        connection = self._required_connection()
        self._validate_before_unlock(connection)
        passphrase = ""
        try:
            passphrase = self.passphrase()
            self.lifecycle.unlock(passphrase)
        except (AtlasLifecycleError, SecretStoreError) as exc:
            error_type = exc.error_type if isinstance(exc, AtlasLifecycleError) else "secret_store_unavailable"
            raise AtlasSettingsError(error_type, "Atlas could not be unlocked") from None
        finally:
            passphrase = ""
        return self.status()

    def startup(self) -> None:
        try:
            self.lifecycle.start()
        except AtlasLifecycleError:
            return
        self._auto_unlock_after_start()

    def restart(self) -> AtlasSettingsStatus:
        try:
            self.lifecycle.restart()
        except AtlasLifecycleError:
            return self.status()
        self._auto_unlock_after_start()
        return self.status()

    def save_directory_and_restart(self, directory: Path) -> AtlasSettingsStatus:
        if not directory.is_absolute():
            raise AtlasSettingsError("invalid_directory", "Atlas directory must be absolute")
        try:
            resolved = self.lifecycle.validate_directory(directory)
        except AtlasLifecycleError as exc:
            raise AtlasSettingsError(exc.error_type, str(exc)) from None
        if resolved == self.lifecycle.directory:
            return self.restart()
        connection = self._connection()
        key_reference = connection.secret_reference if connection else None
        passphrase_reference = connection.passphrase_secret_reference if connection else None
        old_directory = self.lifecycle.directory
        if self.lifecycle.ownership == "owned":
            self.lifecycle.stop()
        try:
            self.lifecycle.save_directory(resolved)
            if connection is not None:
                self._invalidate_authorizations("Atlas directory changed")
                self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.lifecycle.save_directory(old_directory)
            raise AtlasSettingsError("internal_failure", "Atlas directory could not be changed safely") from None
        if key_reference:
            self._best_effort_delete(key_reference, ATLAS_KEY_NAMESPACE)
        if passphrase_reference:
            self._best_effort_delete(passphrase_reference, ATLAS_PASSPHRASE_NAMESPACE)
        try:
            self.lifecycle.start()
        except AtlasLifecycleError:
            pass
        return self.status()

    def _auto_unlock_after_start(self) -> None:
        connection = self._connection()
        if connection is None or not self._passphrase_configured(connection):
            return
        self.lifecycle.auto_unlock_attempted = True
        try:
            self.unlock_now()
        except AtlasSettingsError as exc:
            self.lifecycle.startup_error = str(exc)

    def api_key(self) -> str:
        connection = self._required_connection()
        store = self._require_store(connection.secret_store_id)
        try:
            return store.get(connection.secret_reference, namespace=ATLAS_KEY_NAMESPACE)
        except SecretStoreError:
            raise AtlasSettingsError("secret_store_unavailable", "Stored Atlas API key is unavailable") from None

    def passphrase(self) -> str:
        connection = self._required_connection()
        if not self._passphrase_configured(connection):
            raise AtlasSettingsError("passphrase_missing", "Automatic unlock is not configured")
        store = self._require_store(connection.passphrase_secret_store_id)
        return store.get(connection.passphrase_secret_reference, namespace=ATLAS_PASSPHRASE_NAMESPACE)

    def _validate_before_unlock(self, connection: IntegrationConnection) -> None:
        if self.lifecycle.ownership != "external":
            return
        try:
            status = self.lifecycle.validate_api_key(self.api_key())
        except (AtlasLifecycleError, AtlasSettingsError) as exc:
            error_type = getattr(exc, "error_type", "external_identity_unverified")
            raise AtlasSettingsError(error_type, "External Atlas identity could not be verified") from None
        if status not in {"connected", "locked_recognized"}:
            raise AtlasSettingsError("external_identity_unverified", "External Atlas identity could not be verified")

    def _connection(self) -> IntegrationConnection | None:
        return self.db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "atlas"))

    def _required_connection(self) -> IntegrationConnection:
        connection = self._connection()
        if connection is None:
            raise AtlasSettingsError("api_key_missing", "A validated Atlas API key is required")
        return connection

    def _require_store(self, expected_id: str | None = None) -> SecretStore:
        if self.secret_store is None or (expected_id is not None and self.secret_store.implementation_id != expected_id):
            raise AtlasSettingsError("secret_store_unavailable", "Operating-system secret storage is unavailable")
        return self.secret_store

    def _store_matches(self, expected_id: str) -> bool:
        return self.secret_store is not None and self.secret_store.implementation_id == expected_id

    @staticmethod
    def _bounded_secret(value: str, label: str) -> None:
        if not value or len(value) > 4096:
            raise AtlasSettingsError("invalid_input", f"{label} must be a non-empty bounded string")

    @staticmethod
    def _passphrase_configured(connection: IntegrationConnection | None) -> bool:
        return bool(connection and connection.passphrase_secret_store_id and connection.passphrase_secret_reference)

    def _best_effort_delete(self, reference: str, namespace: str) -> None:
        if self.secret_store is None:
            return
        try:
            self.secret_store.delete(reference, namespace=namespace)
        except SecretStoreError:
            pass

    def _invalidate_authorizations(self, reason: str) -> None:
        now = utc_now()
        authorizations = self.db.scalars(
            select(IntegrationAuthorization)
            .where(IntegrationAuthorization.provider == "atlas")
            .where(IntegrationAuthorization.invalidated_at.is_(None))
        ).all()
        for authorization in authorizations:
            authorization.invalidated_at = now
            authorization.invalidation_reason = reason
            if authorization.approval_request.status in {"pending", "approved"}:
                authorization.approval_request.status = "superseded"


def build_default_atlas_settings_service(db: Session) -> AtlasSettingsService:
    try:
        store = default_secret_store()
    except SecretStoreError:
        store = None
    return AtlasSettingsService(db, secret_store=store)


def atlas_connection_status(db: Session) -> AtlasSettingsStatus:
    return build_default_atlas_settings_service(db).status()


def get_atlas_api_key(db: Session) -> str:
    return build_default_atlas_settings_service(db).api_key()
