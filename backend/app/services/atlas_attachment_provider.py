"""Bounded Atlas file transfers using the existing shared download directory."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from jsonschema import Draft202012Validator

from app.integrations.atlas_attachments import ATLAS_ATTACHMENT_SPECS
from app.services.act_download_service import ActDownloadError, _publish_unique, _safe_name
from app.services.act_workspace_service import ActWorkspaceError, checked_workspace_path, ensure_act_workspace
from app.services.atlas_read_provider import _binary, _segment
from app.services.github_provider import IntegrationProviderError

_SPECS = {spec.id: spec for spec in ATLAS_ATTACHMENT_SPECS}
_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
          ".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _error(kind: str = "invalid_input") -> IntegrationProviderError:
    return IntegrationProviderError(kind, "Atlas attachment operation could not be completed")


class AtlasAttachmentProvider:
    def __init__(self, request):
        self.request = request

    def execute(self, operation_id: str, requested: dict) -> dict:
        spec = _SPECS.get(operation_id)
        if spec is None or not Draft202012Validator(spec.input_schema).is_valid(requested):
            raise _error()
        try:
            if operation_id == "atlas.attachments.upload":
                return self._upload(requested)
            kind, identity = requested["kind"], _segment(requested["id"])
            if operation_id == "atlas.attachments.delete":
                path = f"/api/images/{identity}" if kind == "image" else f"/api/records/{identity}/resume"
                result = self.request(path, None, method="DELETE", timeout=15, max_bytes=100_000)
                if not isinstance(result, dict) or result.get("deleted") is not True:
                    raise _error("provider_unavailable")
                return {"result": result}
            return self._download(kind, identity)
        except (OSError, ValueError, ActWorkspaceError, ActDownloadError):
            raise _error() from None

    def _upload(self, requested: dict) -> dict:
        reference = requested["file"]
        relative = reference["path"]
        parts = PurePosixPath(relative).parts
        if (len(parts) != 3 or parts[:2] != ("workspace", "downloads") or "\\" in relative
                or relative != "/".join(parts) or any(part in {".", ".."} or ":" in part for part in parts)
                or reference["filename"] != parts[-1]):
            raise _error()
        workspace = ensure_act_workspace()
        path = checked_workspace_path(workspace.root, workspace.root.joinpath(*parts))
        if not path.is_file():
            raise _error()
        kind = requested["kind"]
        maximum = (20 if kind == "image" else 10) * 1024 * 1024
        suffix = path.suffix.lower()
        if suffix not in ({".png", ".jpg", ".jpeg", ".webp"} if kind == "image" else {".pdf", ".docx"}):
            raise _error()
        if "media_type" in reference and reference["media_type"] != _MEDIA[suffix]:
            raise _error()
        # Read from a single opened file, bound the actual bytes, then compare the
        # caller's immutable reference before making any provider-side change.
        with path.open("rb") as source:
            raw = source.read(maximum + 1)
        if not raw or len(raw) > maximum or len(raw) != reference["bytes"]:
            raise _error()
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise _error("stale_revision")
        identity = _segment(requested["record_id"])
        path = f"/api/records/{identity}/" + ("images" if kind == "image" else "resume")
        result = self.request(path, raw, method="POST" if kind == "image" else "PUT", timeout=30,
                              max_bytes=100_000, headers={"Content-Type": _MEDIA[suffix],
                              "X-Atlas-Filename": quote(reference["filename"], safe="")})
        if not isinstance(result, dict) or not isinstance(result.get("id" if kind == "image" else "recordId"), str):
            raise _error("provider_unavailable")
        return {"attachment": result}

    def _download(self, kind: str, identity: str) -> dict:
        route = f"/api/images/{identity}/content" if kind == "image" else f"/api/records/{identity}/resume/content"
        raw, media_type = _binary(route)
        if not raw or len(raw) > (20 if kind == "image" else 10) * 1024 * 1024:
            raise _error("response_too_large")
        allowed = {mime: suffix for suffix, mime in _MEDIA.items()
                   if suffix in ({".png", ".jpg", ".webp"} if kind == "image" else {".pdf", ".docx"})}
        if media_type not in allowed:
            raise _error("provider_unavailable")
        name = _safe_name(f"atlas-{kind}-{identity}{allowed[media_type]}")
        workspace = ensure_act_workspace()
        folder = checked_workspace_path(workspace.root, workspace.downloads)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
                temporary = Path(output.name)
                output.write(raw)
            target = _publish_unique(temporary, folder, name)
            temporary = None
            return {"file": {"path": f"workspace/downloads/{target.name}", "filename": target.name,
                             "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "media_type": media_type}}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
