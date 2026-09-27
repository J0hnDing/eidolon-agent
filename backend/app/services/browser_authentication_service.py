from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models import BrowserAuthenticationSecret
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store

SECRET_NAMESPACE = "browser_authentication"
MAX_USERNAME_LENGTH = 256
MAX_PASSWORD_LENGTH = 4096


@dataclass(frozen=True)
class BrowserAuthenticationIdentity:
    id: str
    label: str
    login_origins: tuple[str, ...]
    authenticated_origins: tuple[str, ...]
    username_selectors: tuple[str, ...]
    password_selectors: tuple[str, ...]
    submit_selectors: tuple[str, ...]


BROWSER_AUTHENTICATION_IDENTITIES = {
    "uoft": BrowserAuthenticationIdentity(
        id="uoft",
        label="U of T Weblogin / Quercus",
        login_origins=(
            "https://idpz.utorauth.utoronto.ca",
            "https://weblogin.utoronto.ca",
        ),
        authenticated_origins=("https://q.utoronto.ca",),
        username_selectors=(
            'input[name="user"]',
            'input[name="username"]',
            'input[id="username"]',
            'input[autocomplete="username"]',
        ),
        password_selectors=('input[type="password"]',),
        submit_selectors=('button[type="submit"]', 'input[type="submit"]'),
    )
}


class BrowserAuthenticationError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(" ".join(message.split())[:256])
        self.error_type = error_type


@dataclass(frozen=True)
class BrowserCredentials:
    username: str
    password: str


class BrowserAuthenticationBridge(Protocol):
    def status(self, identity: BrowserAuthenticationIdentity) -> dict[str, str]: ...

    def authenticate(
        self,
        identity: BrowserAuthenticationIdentity,
        credentials: BrowserCredentials,
    ) -> dict[str, str]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalized_origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold()
    try:
        port = parsed.port
    except ValueError:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = "" if port in {None, default_port} else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


class BrowserAuthenticationService:
    def __init__(self, db: Session, *, secret_store: SecretStore | None = None) -> None:
        self.db = db
        self.secret_store = secret_store or default_secret_store()

    def list_status(self) -> list[dict]:
        rows = {
            row.identity_id: row
            for row in self.db.query(BrowserAuthenticationSecret).all()
        }
        return [self._status(identity, rows.get(identity.id)) for identity in BROWSER_AUTHENTICATION_IDENTITIES.values()]

    def put(self, identity_id: str, username: str, password: str) -> dict:
        identity = self._identity(identity_id)
        clean_username = username.strip()
        if not clean_username or len(clean_username) > MAX_USERNAME_LENGTH:
            raise BrowserAuthenticationError("invalid_input", "Username must be a non-empty bounded string")
        if not password or len(password) > MAX_PASSWORD_LENGTH:
            raise BrowserAuthenticationError("invalid_input", "Password must be a non-empty bounded string")
        payload = json.dumps(
            {"username": clean_username, "password": password},
            separators=(",", ":"),
        )
        try:
            new_reference = self.secret_store.put(payload, namespace=SECRET_NAMESPACE)
        except SecretStoreError:
            raise BrowserAuthenticationError(
                "secret_store_unavailable",
                "Browser authentication secret could not be stored safely",
            ) from None
        payload = ""
        row = self.db.get(BrowserAuthenticationSecret, identity.id)
        old_reference = row.secret_reference if row is not None else None
        try:
            if row is None:
                row = BrowserAuthenticationSecret(
                    identity_id=identity.id,
                    secret_store_id=self.secret_store.implementation_id,
                    secret_reference=new_reference,
                )
                self.db.add(row)
            else:
                row.secret_store_id = self.secret_store.implementation_id
                row.secret_reference = new_reference
                row.updated_at = utc_now()
            self.db.commit()
        except Exception:
            self.db.rollback()
            try:
                self.secret_store.delete(new_reference, namespace=SECRET_NAMESPACE)
            except SecretStoreError:
                pass
            raise BrowserAuthenticationError(
                "internal_failure",
                "Browser authentication secret could not be saved safely",
            ) from None
        if old_reference and old_reference != new_reference:
            try:
                self.secret_store.delete(old_reference, namespace=SECRET_NAMESPACE)
            except SecretStoreError:
                raise BrowserAuthenticationError(
                    "secret_cleanup_failed",
                    "The replacement was saved but the previous secret could not be removed",
                ) from None
        return self._status(identity, row)

    def delete(self, identity_id: str) -> None:
        identity = self._identity(identity_id)
        row = self.db.get(BrowserAuthenticationSecret, identity.id)
        if row is None:
            return
        try:
            self.secret_store.delete(row.secret_reference, namespace=SECRET_NAMESPACE)
        except SecretStoreError:
            raise BrowserAuthenticationError(
                "secret_store_unavailable",
                "Browser authentication secret could not be removed safely",
            ) from None
        self.db.delete(row)
        self.db.commit()

    def credentials(self, identity_id: str) -> BrowserCredentials:
        identity = self._identity(identity_id)
        row = self.db.get(BrowserAuthenticationSecret, identity.id)
        if row is None or row.secret_store_id != self.secret_store.implementation_id:
            raise BrowserAuthenticationError(
                "credential_missing",
                "Browser authentication is not configured for this identity",
            )
        try:
            raw = self.secret_store.get(row.secret_reference, namespace=SECRET_NAMESPACE)
            payload = json.loads(raw)
        except (SecretStoreError, json.JSONDecodeError, TypeError):
            raise BrowserAuthenticationError(
                "credential_unavailable",
                "The stored browser authentication secret is unavailable",
            ) from None
        finally:
            raw = "" if "raw" in locals() else ""
        username = payload.get("username") if isinstance(payload, dict) else None
        password = payload.get("password") if isinstance(payload, dict) else None
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise BrowserAuthenticationError(
                "credential_unavailable",
                "The stored browser authentication secret is unavailable",
            )
        return BrowserCredentials(username=username, password=password)

    @staticmethod
    def _identity(identity_id: str) -> BrowserAuthenticationIdentity:
        identity = BROWSER_AUTHENTICATION_IDENTITIES.get(identity_id)
        if identity is None:
            raise BrowserAuthenticationError("unsupported_identity", "Browser authentication identity is unsupported")
        return identity

    @staticmethod
    def _status(identity: BrowserAuthenticationIdentity, row: BrowserAuthenticationSecret | None) -> dict:
        return {
            "id": identity.id,
            "label": identity.label,
            "configured": row is not None,
            "login_origins": list(identity.login_origins),
            "updated_at": row.updated_at if row is not None else None,
        }


class BrowserAuthenticationCapability:
    def __init__(
        self,
        db: Session,
        *,
        bridge: BrowserAuthenticationBridge,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.service = BrowserAuthenticationService(db, secret_store=secret_store)
        self.bridge = bridge

    def authenticate(self, identity_id: str) -> dict[str, str]:
        identity = self.service._identity(identity_id)
        state = self.bridge.status(identity)
        origin = normalized_origin(state.get("url", ""))
        if origin in identity.authenticated_origins:
            return {"identity": identity.id, "status": "already_authenticated"}
        if state.get("status") == "mfa_required":
            return {"identity": identity.id, "status": "mfa_required"}
        if origin not in identity.login_origins:
            return {"identity": identity.id, "status": "unsupported_origin"}
        credentials = self.service.credentials(identity.id)
        result = self.bridge.authenticate(identity, credentials)
        status = result.get("status", "authentication_failed")
        if status not in {
            "authenticated",
            "already_authenticated",
            "mfa_required",
            "authentication_failed",
            "user_action_required",
        }:
            status = "authentication_failed"
        return {"identity": identity.id, "status": status}


def build_default_browser_authentication_service(db: Session) -> BrowserAuthenticationService:
    return BrowserAuthenticationService(db)
