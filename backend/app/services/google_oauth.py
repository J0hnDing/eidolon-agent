from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.services.github_provider import IntegrationProviderError

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
MAX_OAUTH_RESPONSE_BYTES = 1_000_000
GOOGLE_OAUTH_CLIENT_SECRET_NAMESPACE = "google_oauth"

GoogleRequestJson = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class PendingGoogleOAuth:
    client_id: str
    client_secret: str
    expires_at: float


class GoogleOAuthStateStore:
    """Small, process-local store for single-use OAuth authorization state."""

    def __init__(self, *, ttl_seconds: int = 600, max_pending: int = 8) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self._pending: dict[str, PendingGoogleOAuth] = {}
        self._lock = threading.Lock()

    def create(self, client_id: str, client_secret: str) -> str:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            while len(self._pending) >= self.max_pending:
                oldest = min(self._pending, key=lambda state: self._pending[state].expires_at)
                self._pending.pop(oldest, None)
            state = secrets.token_urlsafe(32)
            self._pending[state] = PendingGoogleOAuth(
                client_id=client_id,
                client_secret=client_secret,
                expires_at=now + self.ttl_seconds,
            )
        return state

    def consume(self, state: str) -> PendingGoogleOAuth:
        now = time.monotonic()
        with self._lock:
            pending = self._pending.pop(state, None)
            self._prune(now)
        if pending is None or pending.expires_at <= now:
            raise IntegrationProviderError("invalid_credential", "Google OAuth state is invalid or expired")
        return pending

    def _prune(self, now: float) -> None:
        for state in [key for key, item in self._pending.items() if item.expires_at <= now]:
            self._pending.pop(state, None)


def google_authorization_url(
    client_id: str,
    state: str,
    *,
    redirect_uri: str,
    scopes: tuple[str, ...],
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "false",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTHORIZATION_URL}?{query}"


def exchange_google_oauth_code(
    request_json: GoogleRequestJson,
    pending: PendingGoogleOAuth,
    code: str,
    *,
    redirect_uri: str,
    required_scope: str,
    permission_name: str,
) -> dict[str, str]:
    payload = request_json(
        GOOGLE_TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode(
            {
                "client_id": pending.client_id,
                "client_secret": pending.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }
        ).encode("utf-8"),
        timeout=15,
        max_bytes=MAX_OAUTH_RESPONSE_BYTES,
        oauth_request=True,
    )
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    granted_scope = payload.get("scope")
    if not isinstance(access_token, str) or not access_token:
        raise IntegrationProviderError("invalid_credential", "Google OAuth did not return an access token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise IntegrationProviderError("invalid_credential", "Google OAuth did not return a refresh token")
    scopes = set(granted_scope.split()) if isinstance(granted_scope, str) else set()
    if required_scope not in scopes:
        raise IntegrationProviderError("provider_forbidden", f"{permission_name} permission was not granted")
    return {"access_token": access_token, "refresh_token": refresh_token}


def google_identity(request_json: GoogleRequestJson, access_token: str) -> dict[str, str]:
    payload = request_json(
        GOOGLE_USERINFO_URL,
        method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
        body=None,
        timeout=10,
        max_bytes=MAX_OAUTH_RESPONSE_BYTES,
    )
    subject = payload.get("sub")
    email = payload.get("email")
    verified = payload.get("email_verified")
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(email, str)
        or not email
        or verified is not True
    ):
        raise IntegrationProviderError("provider_unavailable", "Google returned an invalid account identity")
    account_id = hashlib.sha256(f"google:{subject}".encode()).hexdigest()
    return {"account_id": account_id, "email": email}


def parse_google_oauth_credential(credential: str) -> dict[str, str]:
    try:
        value = json.loads(credential)
    except (json.JSONDecodeError, TypeError):
        raise IntegrationProviderError("invalid_credential", "Stored Google OAuth credential is invalid") from None
    required = {"client_id", "client_secret", "refresh_token"}
    if not isinstance(value, dict) or set(value) != required:
        raise IntegrationProviderError("invalid_credential", "Stored Google OAuth credential is invalid")
    if not all(isinstance(value[key], str) and value[key] for key in required):
        raise IntegrationProviderError("invalid_credential", "Stored Google OAuth credential is invalid")
    return value


def serialize_google_oauth_client(client_id: str, client_secret: str) -> str:
    if not 1 <= len(client_id) <= 1024 or not 1 <= len(client_secret) <= 4096:
        raise IntegrationProviderError("invalid_credential", "Stored Google OAuth client is invalid")
    return json.dumps(
        {"client_id": client_id, "client_secret": client_secret},
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_google_oauth_client(credential: str) -> dict[str, str]:
    try:
        value = json.loads(credential)
    except (json.JSONDecodeError, TypeError):
        raise IntegrationProviderError("invalid_credential", "Stored Google OAuth client is invalid") from None
    required = {"client_id", "client_secret"}
    if not isinstance(value, dict) or set(value) != required:
        raise IntegrationProviderError("invalid_credential", "Stored Google OAuth client is invalid")
    if not all(isinstance(value[key], str) and value[key] for key in required):
        raise IntegrationProviderError("invalid_credential", "Stored Google OAuth client is invalid")
    return value


def refresh_google_access_token(
    request_json: GoogleRequestJson,
    bundle: dict[str, str],
) -> str:
    payload = request_json(
        GOOGLE_TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode(
            {
                "client_id": bundle["client_id"],
                "client_secret": bundle["client_secret"],
                "refresh_token": bundle["refresh_token"],
                "grant_type": "refresh_token",
            }
        ).encode("utf-8"),
        timeout=15,
        max_bytes=MAX_OAUTH_RESPONSE_BYTES,
        oauth_request=True,
    )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise IntegrationProviderError("invalid_credential", "Google OAuth refresh failed")
    return access_token
