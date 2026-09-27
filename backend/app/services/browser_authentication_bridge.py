from __future__ import annotations

import ctypes
import json
import os
import socket
from pathlib import Path
from typing import BinaryIO

from app.services.browser_authentication_service import (
    BrowserAuthenticationError,
    BrowserAuthenticationIdentity,
    BrowserCredentials,
)

MAX_BRIDGE_RESPONSE_BYTES = 16 * 1024


class BrowserAuthenticationBridgeClient:
    def __init__(self, endpoint: str | None = None, token: str | None = None) -> None:
        self.endpoint = endpoint or os.environ.get("EIDOLON_BROWSER_AUTH_ENDPOINT", "")
        self.token = token or os.environ.get("EIDOLON_BROWSER_AUTH_TOKEN", "")

    def status(self, identity: BrowserAuthenticationIdentity) -> dict[str, str]:
        return self._request(
            {
                "action": "status",
                "identity": identity.id,
                "loginOrigins": list(identity.login_origins),
                "authenticatedOrigins": list(identity.authenticated_origins),
            }
        )

    def authenticate(
        self,
        identity: BrowserAuthenticationIdentity,
        credentials: BrowserCredentials,
    ) -> dict[str, str]:
        return self._request(
            {
                "action": "authenticate",
                "identity": identity.id,
                "loginOrigins": list(identity.login_origins),
                "authenticatedOrigins": list(identity.authenticated_origins),
                "usernameSelectors": list(identity.username_selectors),
                "passwordSelectors": list(identity.password_selectors),
                "submitSelectors": list(identity.submit_selectors),
                "username": credentials.username,
                "password": credentials.password,
            }
        )

    def _request(self, payload: dict) -> dict[str, str]:
        if not self.endpoint or not self.token:
            raise BrowserAuthenticationError(
                "browser_unavailable",
                "Browser authentication bridge is unavailable",
            )
        request = json.dumps({"token": self.token, **payload}, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            if os.name == "nt" and self.endpoint.startswith("\\\\.\\pipe\\"):
                response = self._request_named_pipe(request)
            else:
                response = self._request_unix_socket(request)
            parsed = json.loads(response.decode("utf-8"))
        except BrowserAuthenticationError:
            raise
        except Exception:
            raise BrowserAuthenticationError(
                "browser_unavailable",
                "Browser authentication bridge is unavailable",
            ) from None
        if not isinstance(parsed, dict):
            raise BrowserAuthenticationError(
                "browser_unavailable",
                "Browser authentication bridge returned an invalid response",
            )
        status = parsed.get("status")
        if not isinstance(status, str):
            raise BrowserAuthenticationError(
                "browser_unavailable",
                "Browser authentication bridge returned an invalid response",
            )
        result = {"status": status}
        if isinstance(parsed.get("url"), str):
            result["url"] = parsed["url"]
        return result

    def _request_named_pipe(self, request: bytes) -> bytes:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait_named_pipe = kernel32.WaitNamedPipeW
        wait_named_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        wait_named_pipe.restype = ctypes.c_int
        if not wait_named_pipe(self.endpoint, 5_000):
            raise BrowserAuthenticationError(
                "browser_unavailable",
                "Browser authentication bridge is unavailable",
            )
        with open(self.endpoint, "r+b", buffering=0) as pipe:  # noqa: PTH123
            pipe.write(request)
            return self._readline(pipe)

    def _request_unix_socket(self, request: bytes) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(self.endpoint)
            client.sendall(request)
            return self._readline(client.makefile("rb"))

    @staticmethod
    def _readline(stream: BinaryIO) -> bytes:
        response = stream.readline(MAX_BRIDGE_RESPONSE_BYTES + 1)
        if not response or len(response) > MAX_BRIDGE_RESPONSE_BYTES:
            raise BrowserAuthenticationError(
                "browser_unavailable",
                "Browser authentication bridge returned an invalid response",
            )
        return response


def browser_authentication_bridge_endpoint(token: str, runtime_root: Path) -> str:
    suffix = token[:24].replace("-", "").replace("_", "")
    if os.name == "nt":
        return rf"\\.\pipe\eidolon-browser-auth-{suffix}"
    path = runtime_root / f"browser-auth-{suffix}.sock"
    return str(path)
