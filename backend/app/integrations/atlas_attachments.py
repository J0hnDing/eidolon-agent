"""Atlas attachment file operations; content never travels in tool JSON."""

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ResourceSpec,
    RiskLevel,
)


def _object(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_KIND = {"enum": ["image", "resume"]}
_FILE = _object({
    "path": {"type": "string", "minLength": 1, "maxLength": 500,
             "description": "Relative workspace/downloads/<filename> reference; no absolute paths or links."},
    "filename": {"type": "string", "minLength": 1, "maxLength": 200},
    "bytes": {"type": "integer", "minimum": 1, "maximum": 20 * 1024 * 1024},
    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "media_type": {"type": "string", "maxLength": 200},
}, ("path", "filename", "bytes", "sha256"))
_DOWNLOADED_FILE = _object({**_FILE["properties"], "media_type": {"type": "string"}},
                           (*_FILE["required"], "media_type"))


def _spec(name: str, description: str, inputs: dict, required: tuple, output: dict,
          effects: frozenset, risk: RiskLevel, example: dict, identity: tuple) -> IntegrationOperationSpec:
    return IntegrationOperationSpec(
        id=f"atlas.attachments.{name}", title=f"Atlas attachments {name}", description=description,
        input_schema=_object(inputs, required), output_schema=output, effects=effects,
        resource=ResourceSpec("atlas.attachments", identity), risk=risk,
        presentation=OperationPresentation(
            usage_example={"operation": f"atlas.attachments.{name}", "input": example},
            normalized_errors=("invalid_input", "atlas_locked", "not_found", "stale_revision",
                               "response_too_large", "provider_unavailable", "provider_timeout",
                               "authorization_missing_or_stale"),
        ),
    )


ATLAS_ATTACHMENT_SPECS = (
    _spec("upload", "Upload an Experience image (JPEG/PNG/WebP, up to 20 MiB), or create/replace a Person "
          "resume (PDF/DOCX, up to 10 MiB). file must reference a regular file directly in the shared "
          "workspace/downloads directory, with its exact filename, byte count and SHA-256. "
          "Atlas validates content and category. Image updates use delete then upload; resume upload replaces "
          "the existing singleton. This does not create record revisions.",
          {"kind": _KIND, "record_id": _ID, "file": _FILE}, ("kind", "record_id", "file"),
          _object({"attachment": {"type": "object"}}, ("attachment",)),
          frozenset({IntegrationEffect.CREATE, IntegrationEffect.UPDATE}), RiskLevel.MEDIUM,
          {"kind": "image", "record_id": "experience-id", "file": {
              "path": "workspace/downloads/photo.png", "filename": "photo.png", "bytes": 1024, "sha256": "a" * 64}},
          ("record_id",)),
    _spec("delete", "Permanently delete an Experience image (id=image ID) or Person resume (id=Person "
          "record ID). Attachment bytes cannot be restored through record history. Requires per-call approval.",
          {"kind": _KIND, "id": _ID}, ("kind", "id"),
          _object({"result": {"type": "object"}}, ("result",)),
          frozenset({IntegrationEffect.DELETE}), RiskLevel.HIGH,
          {"kind": "image", "id": "image-id"}, ("id",)),
    _spec("download", "Save original image or resume bytes to a new unique file in workspace/downloads. "
          "id is the image ID for image, or Person record ID for resume. Returns a controlled file reference "
          "and SHA-256 suitable for upload or file delivery; no arbitrary destination path is accepted.",
          {"kind": _KIND, "id": _ID}, ("kind", "id"),
          _object({"file": _DOWNLOADED_FILE}, ("file",)),
          frozenset({IntegrationEffect.READ}), RiskLevel.LOW,
          {"kind": "resume", "id": "person-id"}, ("id",)),
)
ATLAS_ATTACHMENT_OPERATIONS = frozenset(spec.id for spec in ATLAS_ATTACHMENT_SPECS)
