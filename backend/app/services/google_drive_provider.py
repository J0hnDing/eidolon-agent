"""Trusted, bounded Google Drive transport and local file boundary."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from app.services.act_download_service import (
    ALLOWED_MEDIA_TYPES,
    ALLOWED_SUFFIXES,
    MAX_DOWNLOAD_BYTES,
    _publish_unique,
    _safe_name,
    _validate_content,
)
from app.services.act_workspace_service import ensure_act_workspace
from app.services.github_provider import IntegrationProviderError
from app.services.google_calendar_provider import UrllibGoogleCalendarProviderAdapter
from app.services.google_oauth import (
    GoogleOAuthStateStore,
    PendingGoogleOAuth,
    exchange_google_oauth_code,
    google_authorization_url,
    google_identity,
    parse_google_oauth_credential,
    refresh_google_access_token,
)
from app.services.local_document_reader import LocalDocumentReadError, read_local_document

GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_DRIVE_SCOPES = ("openid", "email", GOOGLE_DRIVE_SCOPE)
GOOGLE_DRIVE_REDIRECT_URI = "http://localhost:8000/settings/integrations/google-drive/oauth/callback"
GOOGLE_DRIVE_SECRET_NAMESPACE = "google_drive"
GOOGLE_DRIVE_BASE = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"
GOOGLE_DRIVE_DOWNLOAD_FOLDER = "google drive download"
GOOGLE_NATIVE_EXPORTS = {
    "application/vnd.google-apps.document": {"docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"), "pdf": ("application/pdf", ".pdf"), "txt": ("text/plain", ".txt")},
    "application/vnd.google-apps.spreadsheet": {"xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"), "pdf": ("application/pdf", ".pdf"), "csv": ("text/csv", ".csv")},
    "application/vnd.google-apps.presentation": {"pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"), "pdf": ("application/pdf", ".pdf")},
}
GOOGLE_DRIVE_FIELDS = "id,name,mimeType,parents,modifiedTime,webViewLink"
google_drive_oauth_state_store = GoogleOAuthStateStore()


class GoogleDriveClient(UrllibGoogleCalendarProviderAdapter):
    """Drive endpoints using the shared Google OAuth and bounded HTTP transport."""

    def authorization_url(self, client_id: str, state: str) -> str:
        return google_authorization_url(client_id, state, redirect_uri=GOOGLE_DRIVE_REDIRECT_URI, scopes=GOOGLE_DRIVE_SCOPES)

    def exchange_code(self, pending: PendingGoogleOAuth, code: str) -> dict[str, str]:
        return exchange_google_oauth_code(self._request_json, pending, code, redirect_uri=GOOGLE_DRIVE_REDIRECT_URI, required_scope=GOOGLE_DRIVE_SCOPE, permission_name="Google Drive")

    def identity(self, access_token: str) -> dict[str, str]:
        return google_identity(self._request_json, access_token)

    def execute(self, operation_id: str, values: dict, credential: str) -> dict:
        token = refresh_google_access_token(self._request_json, parse_google_oauth_credential(credential))
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if operation_id == "google_drive.search":
            filters = ["trashed = false"]
            if values.get("query"):
                filters.append(f"name contains '{self._escape(values['query'])}'")
            if values.get("parent_id"):
                filters.append(f"'{self._escape(values['parent_id'])}' in parents")
            if values.get("mime_type"):
                filters.append(f"mimeType = '{self._escape(values['mime_type'])}'")
            url = GOOGLE_DRIVE_BASE + "?" + urlencode({"q": " and ".join(filters), "pageSize": values.get("limit", 25), "fields": f"nextPageToken,files({GOOGLE_DRIVE_FIELDS})"})
            result = self._request_json(url, method="GET", headers=headers, body=None, timeout=15, max_bytes=1_000_000)
            files = result.get("files")
            if not isinstance(files, list):
                raise IntegrationProviderError("provider_unavailable", "Drive returned invalid search results")
            return {"files": [self._metadata(item) for item in files[:values.get("limit", 25)]]}
        if operation_id == "google_drive.upload":
            return self._upload(values, headers)
        if operation_id in {"google_drive.download", "google_drive.read"}:
            metadata = self._get_metadata(values["file_id"], headers)
            local = self._download(metadata, values.get("export_format"), headers)
            if operation_id == "google_drive.read":
                try:
                    text = read_local_document(ensure_act_workspace().root / local["path"])
                except LocalDocumentReadError:
                    raise IntegrationProviderError("unsupported_file_type", "Drive file has no readable text") from None
                return {"metadata": metadata, "text": text}
            return {"file": local, "metadata": metadata}
        raise IntegrationProviderError("operation_undeclared", "Google Drive operation is not implemented")

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _metadata(item: object) -> dict:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("name"), str) or not isinstance(item.get("mimeType"), str):
            raise IntegrationProviderError("provider_unavailable", "Drive returned invalid file metadata")
        return {"id": item["id"], "name": item["name"], "mime_type": item["mimeType"], "parents": item.get("parents") if isinstance(item.get("parents"), list) else [], "modified_time": item.get("modifiedTime") if isinstance(item.get("modifiedTime"), str) else None, "web_url": item.get("webViewLink") if isinstance(item.get("webViewLink"), str) else None}

    def _get_metadata(self, file_id: str, headers: dict) -> dict:
        url = f"{GOOGLE_DRIVE_BASE}/{quote(file_id, safe='')}?{urlencode({'fields': GOOGLE_DRIVE_FIELDS})}"
        return self._metadata(self._request_json(url, method="GET", headers=headers, body=None, timeout=15, max_bytes=100_000))

    def _download(self, metadata: dict, export_format: str | None, headers: dict) -> dict:
        mime = metadata["mime_type"]
        file_id = quote(metadata["id"], safe="")
        if mime in GOOGLE_NATIVE_EXPORTS:
            formats = GOOGLE_NATIVE_EXPORTS[mime]
            format_name = export_format or next(iter(formats))
            if format_name not in formats:
                raise IntegrationProviderError("invalid_input", "Export format is not supported for this Drive file")
            media_type, suffix = formats[format_name]
            url = f"{GOOGLE_DRIVE_BASE}/{file_id}/export?{urlencode({'mimeType': media_type})}"
            name = metadata["name"] if metadata["name"].lower().endswith(suffix) else metadata["name"] + suffix
        else:
            if export_format is not None or mime.startswith("application/vnd.google-apps."):
                raise IntegrationProviderError("unsupported_file_type", "Drive file cannot be exported")
            name = metadata["name"]
            suffix = Path(name).suffix.lower()
            media_type = mime
            url = f"{GOOGLE_DRIVE_BASE}/{file_id}?alt=media"
        if suffix not in ALLOWED_SUFFIXES or (media_type not in ALLOWED_MEDIA_TYPES[suffix] and media_type != "application/octet-stream"):
            raise IntegrationProviderError("unsupported_file_type", "Drive file type is not supported")
        raw = self._request_bytes(url, method="GET", headers={"Authorization": headers["Authorization"]}, body=None, timeout=30, max_bytes=MAX_DOWNLOAD_BYTES)
        folder = ensure_act_workspace().knowledge / GOOGLE_DRIVE_DOWNLOAD_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        try:
            safe_name = _safe_name(name)
            import tempfile
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temporary:
                temporary.write(raw)
                temp = Path(temporary.name)
            try:
                _validate_content(temp, suffix, media_type)
                target = _publish_unique(temp, folder, safe_name)
            finally:
                temp.unlink(missing_ok=True)
        except (OSError, ValueError):
            raise IntegrationProviderError("unsupported_file_type", "Drive file failed local validation") from None
        return {"path": f"knowledge/{GOOGLE_DRIVE_DOWNLOAD_FOLDER}/{target.name}", "filename": target.name, "bytes": len(raw), "media_type": media_type}

    def _upload(self, values: dict, headers: dict) -> dict:
        reference = values["file"]
        relative = reference["path"]
        parts = PurePosixPath(relative).parts
        allowed = parts[:2] == ("workspace", "downloads") or parts[:2] == ("knowledge", GOOGLE_DRIVE_DOWNLOAD_FOLDER)
        if (not allowed or len(parts) != 3 or any(part in {"", ".", ".."} for part in parts)
                or "\\" in relative or relative.startswith("/")):
            raise IntegrationProviderError("invalid_input", "Eidolon file reference is invalid")
        submitted = ensure_act_workspace().root.joinpath(*parts)
        path = submitted.resolve()
        root = ensure_act_workspace().root.resolve()
        if (not path.is_relative_to(root) or not path.is_file() or any(part.is_symlink() for part in (submitted, *submitted.parents))
                or path.stat().st_size > MAX_DOWNLOAD_BYTES):
            raise IntegrationProviderError("invalid_input", "Eidolon file is unavailable or exceeds the size limit")
        if reference["filename"] != path.name or reference["bytes"] != path.stat().st_size:
            raise IntegrationProviderError("invalid_input", "Eidolon file reference does not match the local file")
        name = _safe_name(values.get("name") or path.name)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        metadata = {"name": name}
        if values.get("parent_id"):
            metadata["parents"] = [values["parent_id"]]
        boundary = "eidolon-drive-upload"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + json.dumps(metadata).encode() + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode())
        url = GOOGLE_DRIVE_UPLOAD_BASE + "?" + urlencode({"uploadType": "multipart", "fields": GOOGLE_DRIVE_FIELDS})
        result = self._request_json(url, method="POST", headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"}, body=body, timeout=30, max_bytes=100_000)
        return self._metadata(result)
