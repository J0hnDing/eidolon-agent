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
    if not (skill_dir / "tests").is_dir():
        raise ManifestValidationError("skills require tests/")
    entrypoint_path = (
        manifest.entrypoint
        if manifest.runtime == "function"
        else _web_app_entrypoint_path(skill_dir, manifest.entrypoint)
    )
    for relative_path in (entrypoint_path, manifest.instructions_path):
        if relative_path is None:
            continue
        resolved = (skill_dir / relative_path).resolve()
        if not resolved.is_relative_to(skill_dir.resolve()):
            raise ManifestValidationError("Declared skill files must stay inside the skill folder")
        if not resolved.is_file():
            raise ManifestValidationError(f"Declared file is missing: {relative_path}")


def _web_app_entrypoint_path(skill_dir: Path, entrypoint: str) -> str:
    module, _, _attribute = entrypoint.partition(":")
    module_path = module.replace(".", "/")
    file_candidate = skill_dir / f"{module_path}.py"
    package_candidate = skill_dir / module_path / "__init__.py"
    if file_candidate.is_file():
        return f"{module_path}.py"
    if package_candidate.is_file():
        return f"{module_path}/__init__.py"
    raise ManifestValidationError(f"Declared web_app module is missing: {module}")


__all__ = [
    "ManifestValidationError",
    "SkillManifest",
    "classify_permission_risk",
    "validate_manifest",
    "validate_manifest_file",
    "validate_manifest_json",
    "validate_manifest_package",
]
