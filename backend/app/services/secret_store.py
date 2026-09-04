from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4


class SecretStoreError(RuntimeError):
    pass


class SecretStore(Protocol):
    implementation_id: str

    def put(self, secret: str, *, namespace: str = "github") -> str: ...

    def get(self, reference: str, *, namespace: str = "github") -> str: ...

    def delete(self, reference: str, *, namespace: str = "github") -> None: ...


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_uint64),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialSecretStore:
    """Narrow Windows Credential Manager wrapper with no application-owned fallback."""

    implementation_id = "windows_credential_manager"
    _prefix_by_namespace = {
        "github": "Eidolon/GitHub/",  # Preserve existing credential targets.
        "notion": "Eidolon/Notion/",
        "google_calendar": "Eidolon/GoogleCalendar/",
        "gmail": "Eidolon/Gmail/",
        "google_oauth": "Eidolon/GoogleOAuth/",
        "telegram": "Eidolon/Telegram/",
        "quercus": "Eidolon/Quercus/",
        "atlas_api_key": "Eidolon/Atlas/APIKey/",
        "atlas_passphrase": "Eidolon/Atlas/Passphrase/",
    }

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretStoreError("Operating-system secret storage is unsupported")
        try:
            self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        except OSError as exc:
            raise SecretStoreError("Operating-system secret storage is unavailable") from exc
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), ctypes.c_uint32]
        self._advapi.CredWriteW.restype = ctypes.c_int
        self._advapi.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        self._advapi.CredReadW.restype = ctypes.c_int
        self._advapi.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self._advapi.CredDeleteW.restype = ctypes.c_int
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredFree.restype = None

    def put(self, secret: str, *, namespace: str = "github") -> str:
        if not secret:
            raise SecretStoreError("Credential cannot be empty")
        reference = uuid4().hex
        target = f"{self._prefix(namespace)}{reference}"
        blob = secret.encode("utf-8")
        blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _Credential(
            Flags=0,
            Type=1,
            TargetName=target,
            Comment=None,
            LastWritten=0,
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte)),
            Persist=2,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=namespace,
        )
        if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
            raise SecretStoreError("Operating-system secret storage rejected the credential")
        return reference

    def get(self, reference: str, *, namespace: str = "github") -> str:
        target = self._target(reference, namespace)
        pointer = ctypes.POINTER(_Credential)()
        if not self._advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            raise SecretStoreError("Stored credential is unavailable")
        try:
            size = int(pointer.contents.CredentialBlobSize)
            raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
            return raw.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SecretStoreError("Stored credential is unreadable") from exc
        finally:
            self._advapi.CredFree(pointer)

    def delete(self, reference: str, *, namespace: str = "github") -> None:
        target = self._target(reference, namespace)
        if not self._advapi.CredDeleteW(target, 1, 0):
            error = ctypes.get_last_error()
            if error != 1168:  # ERROR_NOT_FOUND: deletion is idempotent.
                raise SecretStoreError("Operating-system secret storage could not remove the credential")

    def _target(self, reference: str, namespace: str) -> str:
        if len(reference) != 32 or any(character not in "0123456789abcdef" for character in reference):
            raise SecretStoreError("Stored credential reference is invalid")
        return f"{self._prefix(namespace)}{reference}"

    def _prefix(self, namespace: str) -> str:
        try:
            return self._prefix_by_namespace[namespace]
        except KeyError as exc:
            raise SecretStoreError("Credential namespace is invalid") from exc


@dataclass
class FakeSecretStore:
    implementation_id: str = "fake_secret_store"
    values: dict[str, str] = field(default_factory=dict)
    fail_put: bool = False
    fail_get: bool = False
    fail_delete: bool = False
    _counter: int = 0

    namespaces: dict[str, str] = field(default_factory=dict)

    def put(self, secret: str, *, namespace: str = "github") -> str:
        if self.fail_put:
            raise SecretStoreError("Fake secret store put failure")
        self._counter += 1
        reference = f"fake-{self._counter:04d}"
        self.values[reference] = secret
        self.namespaces[reference] = namespace
        return reference

    def get(self, reference: str, *, namespace: str = "github") -> str:
        if self.fail_get:
            raise SecretStoreError("Fake secret store get failure")
        try:
            if self.namespaces.get(reference, "github") != namespace:
                raise KeyError(reference)
            return self.values[reference]
        except KeyError as exc:
            raise SecretStoreError("Fake secret is unavailable") from exc

    def delete(self, reference: str, *, namespace: str = "github") -> None:
        if self.fail_delete:
            raise SecretStoreError("Fake secret store delete failure")
        if self.namespaces.get(reference, "github") == namespace:
            self.values.pop(reference, None)
            self.namespaces.pop(reference, None)


def default_secret_store() -> SecretStore:
    return WindowsCredentialSecretStore()
