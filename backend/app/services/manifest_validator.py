import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.manifest import SkillManifest, classify_permission_risk


class ManifestValidationError(ValueError):
    pass


def validate_manifest(data: dict[str, Any]) -> SkillManifest:
    try:
        return SkillManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestValidationError(str(exc)) from exc


def validate_manifest_json(raw_json: str) -> SkillManifest:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"Manifest must be valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ManifestValidationError("Manifest must be a JSON object")
    return validate_manifest(data)


def validate_manifest_file(path: Path) -> SkillManifest:
    manifest = validate_manifest_json(path.read_text(encoding="utf-8"))
    validate_manifest_package(path.parent, manifest)
    return manifest


def validate_manifest_package(skill_dir: Path, manifest: SkillManifest) -> None:
    if manifest.skill_type in {"automation", "hybrid"} and not (skill_dir / "tests").is_dir():
        raise ManifestValidationError(f"{manifest.skill_type} skills require tests/")


__all__ = [
    "ManifestValidationError",
    "SkillManifest",
    "classify_permission_risk",
    "validate_manifest",
    "validate_manifest_file",
    "validate_manifest_json",
    "validate_manifest_package",
]
