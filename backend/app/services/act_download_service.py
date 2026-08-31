from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.act_workspace_service import ensure_act_workspace

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOWED_SUFFIXES = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
}
ALLOWED_MEDIA_TYPES = {
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".txt": {"text/plain"},
    ".md": {"text/plain", "text/markdown"},
    ".csv": {"text/plain", "text/csv", "application/csv"},
    ".json": {"application/json", "text/json", "text/plain"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".gif": {"image/gif"},
    ".webp": {"image/webp"},
}
GENERIC_MEDIA_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
REDIRECT_CODES = {301, 302, 303, 307, 308}


class ActDownloadError(ValueError):
    pass


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def download_document(
    arguments: dict[str, Any],
    *,
    opener=None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> dict[str, Any]:
    source = arguments.get("source")
    if not isinstance(source, dict) or source.get("kind") != "url" or not isinstance(source.get("url"), str):
        raise ActDownloadError("Only a URL download source is supported.")
    url = source["url"].strip()
    parsed = _parse_url(url)
    requested_name = arguments.get("filename")
    if requested_name is not None and not isinstance(requested_name, str):
        raise ActDownloadError("Download filename is invalid.")
    name = _safe_name(requested_name or Path(parsed.path).name or "download")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ActDownloadError("This download file type is not allowed.")

    workspace = ensure_act_workspace()
    client = opener or build_opener(_NoRedirectHandler())
    temp_path: Path | None = None
    try:
        response, _final_url = _open_validated(client, url, resolver)
        with response:
            media_type = _media_type(response.headers.get("Content-Type"))
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                raise ActDownloadError("Download exceeds the 25 MiB limit.")
            with tempfile.NamedTemporaryFile(dir=workspace.downloads, delete=False) as temporary:
                temp_path = Path(temporary.name)
                total = 0
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ActDownloadError("Download exceeds the 25 MiB limit.")
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
        _validate_content(temp_path, suffix, media_type)
        target = _publish_unique(temp_path, workspace.downloads, name)
        temp_path = None
    except ActDownloadError:
        raise
    except Exception as exc:
        raise ActDownloadError("Document download failed.") from exc
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return {
        "path": (Path("downloads") / target.name).as_posix(),
        "filename": target.name,
        "bytes": target.stat().st_size,
        "media_type": media_type or _canonical_media_type(suffix),
    }


def _parse_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ActDownloadError("Download URL must be an absolute HTTP(S) URL.")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ActDownloadError("Download URL must not contain credentials or a fragment.")
    return parsed


def _validate_url(url: str, resolver: Callable[..., Any]):
    parsed = _parse_url(url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ActDownloadError("Download URL port is invalid.") from None
    try:
        addresses = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ActDownloadError("Download host could not be resolved.") from None
    if not addresses:
        raise ActDownloadError("Download host could not be resolved.")
    for address in addresses:
        raw_ip = address[4][0]
        _require_public_ip(raw_ip)
    return parsed


def _require_public_ip(raw_ip: str) -> None:
    try:
        address = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        raise ActDownloadError("Download host resolved to an invalid address.") from None
    if not address.is_global:
        raise ActDownloadError("Download host resolves to a private or local address.")


def _open_validated(client, url: str, resolver: Callable[..., Any]):
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current, resolver)
        request = Request(current, headers={"User-Agent": "Eidolon-Act/1.0", "Accept": "*/*"})
        try:
            response = client.open(request, timeout=30)
        except HTTPError as exc:
            if exc.code not in REDIRECT_CODES:
                raise ActDownloadError(f"Document server returned HTTP {exc.code}.") from None
            location = exc.headers.get("Location")
            exc.close()
            if not location:
                raise ActDownloadError("Document redirect did not include a destination.")
            current = urljoin(current, location)
            continue
        status = int(getattr(response, "status", response.getcode()))
        if status in REDIRECT_CODES:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ActDownloadError("Document redirect did not include a destination.")
            current = urljoin(current, location)
            continue
        if not 200 <= status < 300:
            response.close()
            raise ActDownloadError(f"Document server returned HTTP {status}.")
        final_url = response.geturl() if hasattr(response, "geturl") else current
        _validate_url(final_url, resolver)
        peer_ip = _response_peer_ip(response)
        if peer_ip is not None:
            _require_public_ip(peer_ip)
        return response, final_url
    raise ActDownloadError("Document download followed too many redirects.")


def _response_peer_ip(response) -> str | None:
    candidates = (
        getattr(response, "fp", None),
        getattr(getattr(response, "fp", None), "raw", None),
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
    )
    for candidate in reversed(candidates):
        getpeername = getattr(candidate, "getpeername", None)
        if callable(getpeername):
            try:
                peer = getpeername()
                if isinstance(peer, tuple) and peer:
                    return str(peer[0])
            except OSError:
                pass
    return None


def _validate_content(path: Path, suffix: str, media_type: str) -> None:
    if media_type not in GENERIC_MEDIA_TYPES and media_type not in ALLOWED_MEDIA_TYPES[suffix]:
        raise ActDownloadError("Downloaded content type does not match the requested file type.")
    head = path.read_bytes()[:32]
    if suffix == ".pdf" and not head.startswith(b"%PDF-"):
        raise ActDownloadError("Downloaded file is not a valid PDF.")
    if suffix == ".png" and not head.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ActDownloadError("Downloaded file is not a valid PNG image.")
    if suffix in {".jpg", ".jpeg"} and not head.startswith(b"\xff\xd8\xff"):
        raise ActDownloadError("Downloaded file is not a valid JPEG image.")
    if suffix == ".gif" and not (head.startswith(b"GIF87a") or head.startswith(b"GIF89a")):
        raise ActDownloadError("Downloaded file is not a valid GIF image.")
    if suffix == ".webp" and not (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
        raise ActDownloadError("Downloaded file is not a valid WebP image.")
    if suffix in {".docx", ".xlsx", ".pptx"}:
        _validate_ooxml(path, suffix)
    if suffix in {".txt", ".md", ".csv", ".json"}:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ActDownloadError("Downloaded text file must be valid UTF-8.") from None
        if "\x00" in text:
            raise ActDownloadError("Downloaded text file contains binary data.")
        if suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError:
                raise ActDownloadError("Downloaded file is not valid JSON.") from None


def _validate_ooxml(path: Path, suffix: str) -> None:
    required_prefix = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}[suffix]
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or not any(name.startswith(required_prefix) for name in names):
                raise ActDownloadError("Downloaded Office document has an invalid package structure.")
            if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise ActDownloadError("Downloaded Office document has unsafe package paths.")
    except (zipfile.BadZipFile, OSError):
        raise ActDownloadError("Downloaded file is not a valid Office document.") from None


def _publish_unique(temp_path: Path, directory: Path, name: str) -> Path:
    source = Path(name)
    for index in range(1000):
        candidate_name = name if index == 0 else f"{source.stem} ({index}){source.suffix}"
        candidate = directory / candidate_name
        try:
            os.link(temp_path, candidate)
            temp_path.unlink()
            return candidate
        except FileExistsError:
            continue
    raise ActDownloadError("Download filename has too many existing copies.")


def _safe_name(value: str) -> str:
    name = Path(value).name.strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    if not name or name in {".", ".."} or len(name) > 180:
        raise ActDownloadError("Download filename is invalid.")
    return name


def _media_type(value: str | None) -> str:
    return (value or "").partition(";")[0].strip().lower()


def _canonical_media_type(suffix: str) -> str:
    return sorted(ALLOWED_MEDIA_TYPES[suffix])[0]
