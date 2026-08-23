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

LEGACY_ATLAS_KEY_NAMESPACE = "atlas_api_key"
ATLAS_PASSPHRASE_NAMESPACE = "atlas_passphrase"
PASSPHRASE_CREDENTIAL_KIND = "passphrase"
LEGACY_PASSPHRASE_CREDENTIAL_KIND = "legacy_passphrase"


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
        cleanup_pending = not self._cleanup_legacy_connection()
        runtime = self.lifecycle.status()
        connection = self._connection()
        return AtlasSettingsStatus(
            directory=str(self.lifecycle.directory),
            running=runtime["running"],
            process_ownership=self.lifecycle.ownership,
            initialized=runtime["initialized"],
            locked=runtime["locked"],
            passphrase_configured=self._passphrase_configured(connection),
            startup_error=self.lifecycle.startup_error,
            error_type="legacy_credentials_pending_cleanup" if cleanup_pending else None,
        )

    def put_passphrase(self, passphrase: str) -> AtlasSettingsStatus:
        self._bounded_secret(passphrase, "Atlas passphrase")
        self._require_owned_process()
        self._require_cleanup_complete()
        store = self._require_store()
        try:
            self.lifecycle.unlock(passphrase)
        except AtlasLifecycleError as exc:
            raise AtlasSettingsError(exc.error_type, str(exc)) from None
        try:
            new_reference = store.put(passphrase, namespace=ATLAS_PASSPHRASE_NAMESPACE)
        except SecretStoreError:
            raise AtlasSettingsError(
                "secret_store_unavailable", "Operating-system secret storage is unavailable"
            ) from None
        connection = self._connection()
        previous_reference = connection.secret_reference if connection else None
        now = utc_now()
        try:
            if connection is None:
                connection = IntegrationConnection(
                    provider="atlas",
                    secret_store_id=store.implementation_id,
                    secret_reference=new_reference,
                    credential_kind=PASSPHRASE_CREDENTIAL_KIND,
                    status="connected",
                    account_login="Local Atlas",
                    account_id="local-atlas",
                    created_at=now,
                    updated_at=now,
                    last_validated_at=now,
                )
                self.db.add(connection)
            else:
                connection.secret_store_id = store.implementation_id
                connection.secret_reference = new_reference
                connection.credential_kind = PASSPHRASE_CREDENTIAL_KIND
                connection.passphrase_secret_store_id = None
                connection.passphrase_secret_reference = None
                connection.status = "connected"
                connection.error_type = None
                connection.updated_at = now
                connection.last_validated_at = now
            self.db.commit()
        except Exception:
            self.db.rollback()
            self._best_effort_delete(new_reference, ATLAS_PASSPHRASE_NAMESPACE)
            raise AtlasSettingsError("internal_failure", "Atlas passphrase could not be saved safely") from None
        if previous_reference and previous_reference != new_reference:
            self._best_effort_delete(previous_reference, ATLAS_PASSPHRASE_NAMESPACE)
        return self.status()

    def remove_passphrase(self) -> AtlasSettingsStatus:
        self._require_cleanup_complete()
        connection = self._connection()
        if not self._passphrase_configured(connection):
            return self.status()
        assert connection is not None
        reference = connection.secret_reference
        try:
            self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise AtlasSettingsError("internal_failure", "Atlas passphrase could not be removed safely") from None
        self._best_effort_delete(reference, ATLAS_PASSPHRASE_NAMESPACE)
        return self.status()

    def unlock_now(self) -> AtlasSettingsStatus:
        self._require_owned_process()
        self._require_cleanup_complete()
        passphrase = ""
        try:
            passphrase = self.passphrase()
            self.lifecycle.unlock(passphrase)
        except AtlasLifecycleError as exc:
            raise AtlasSettingsError(exc.error_type, "Atlas could not be unlocked") from None
        except SecretStoreError:
            raise AtlasSettingsError("secret_store_unavailable", "Atlas could not be unlocked") from None
        finally:
            passphrase = ""
        return self.status()

    def startup(self) -> None:
        try:
            self.lifecycle.start()
        except AtlasLifecycleError:
            return
        if not self._cleanup_legacy_connection():
            self.lifecycle.startup_error = "Legacy Atlas credentials are pending cleanup"
            return
        self._auto_unlock_after_start()

    def restart(self) -> AtlasSettingsStatus:
        try:
            self.lifecycle.restart()
        except AtlasLifecycleError:
            return self.status()
        if not self._cleanup_legacy_connection():
            self.lifecycle.startup_error = "Legacy Atlas credentials are pending cleanup"
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
        old_directory = self.lifecycle.directory
        if self.lifecycle.ownership == "owned":
            self.lifecycle.stop()
        try:
            self.lifecycle.save_directory(resolved)
            if connection is not None:
                self._invalidate_authorizations("Atlas directory changed")
                if connection.credential_kind == PASSPHRASE_CREDENTIAL_KIND:
                    connection.credential_kind = LEGACY_PASSPHRASE_CREDENTIAL_KIND
                connection.status = "legacy"
                connection.updated_at = utc_now()
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.lifecycle.save_directory(old_directory)
            raise AtlasSettingsError("internal_failure", "Atlas directory could not be changed safely") from None
        self._cleanup_legacy_connection()
        try:
            self.lifecycle.start()
        except AtlasLifecycleError:
            pass
        return self.status()

    def passphrase(self) -> str:
        connection = self._connection()
        if not self._passphrase_configured(connection):
            raise AtlasSettingsError("passphrase_missing", "Automatic unlock is not configured")
        assert connection is not None
        store = self._require_store(connection.secret_store_id)
        return store.get(connection.secret_reference, namespace=ATLAS_PASSPHRASE_NAMESPACE)

    def _auto_unlock_after_start(self) -> None:
        connection = self._connection()
        if self.lifecycle.ownership != "owned" or not self._passphrase_configured(connection):
            return
        self.lifecycle.auto_unlock_attempted = True
        try:
            self.unlock_now()
        except AtlasSettingsError as exc:
            self.lifecycle.startup_error = str(exc)

    def _require_owned_process(self) -> None:
        if self.lifecycle.ownership != "owned":
            raise AtlasSettingsError(
                "external_unlock_forbidden",
                "An externally launched Atlas must be unlocked through Atlas itself",
            )

    def _connection(self) -> IntegrationConnection | None:
        return self.db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "atlas"))

    def _require_store(self, expected_id: str | None = None) -> SecretStore:
        if self.secret_store is None or (
            expected_id is not None and self.secret_store.implementation_id != expected_id
        ):
            raise AtlasSettingsError(
                "secret_store_unavailable", "Operating-system secret storage is unavailable"
            )
        return self.secret_store

    @staticmethod
    def _bounded_secret(value: str, label: str) -> None:
        if not value or len(value) > 4096:
            raise AtlasSettingsError("invalid_input", f"{label} must be a non-empty bounded string")

    @staticmethod
    def _passphrase_configured(connection: IntegrationConnection | None) -> bool:
        return bool(
            connection
            and connection.credential_kind == PASSPHRASE_CREDENTIAL_KIND
            and connection.secret_store_id
            and connection.secret_reference
        )

    def _require_cleanup_complete(self) -> None:
        if not self._cleanup_legacy_connection():
            raise AtlasSettingsError(
                "legacy_credentials_pending_cleanup",
                "Legacy Atlas credentials must be removed before a passphrase can be saved",
            )

    def _cleanup_legacy_connection(self) -> bool:
        connection = self._connection()
        if connection is None or (
            connection.credential_kind == PASSPHRASE_CREDENTIAL_KIND
            and connection.passphrase_secret_store_id is None
            and connection.passphrase_secret_reference is None
        ):
            return True
        if connection.status != "legacy":
            self._invalidate_authorizations("Legacy Atlas credential migration")
            connection.status = "legacy"
            connection.updated_at = utc_now()
            try:
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise AtlasSettingsError(
                    "internal_failure", "Legacy Atlas credentials could not be marked safely"
                ) from None
        expected_store_ids = {
            value
            for value in (connection.secret_store_id, connection.passphrase_secret_store_id)
            if value
        }
        if (
            self.secret_store is None
            or any(store_id != self.secret_store.implementation_id for store_id in expected_store_ids)
        ):
            return False
        primary_namespace = (
            ATLAS_PASSPHRASE_NAMESPACE
            if connection.credential_kind == LEGACY_PASSPHRASE_CREDENTIAL_KIND
            else LEGACY_ATLAS_KEY_NAMESPACE
        )
        try:
            self.secret_store.delete(connection.secret_reference, namespace=primary_namespace)
            if connection.passphrase_secret_reference:
                self.secret_store.delete(
                    connection.passphrase_secret_reference,
                    namespace=ATLAS_PASSPHRASE_NAMESPACE,
                )
        except SecretStoreError:
            return False
        try:
            self.db.delete(connection)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise AtlasSettingsError(
                "internal_failure", "Legacy Atlas credentials could not be migrated safely"
            ) from None
        return True

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
