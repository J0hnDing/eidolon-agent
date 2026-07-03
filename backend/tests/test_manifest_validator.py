import pytest

import json
from pathlib import Path

from app.services.manifest_validator import (
    ManifestValidationError,
    validate_manifest,
    validate_manifest_file,
)


def valid_manifest() -> dict:
    return {
        "name": "ai_news_digest",
        "description": "Summarizes AI infrastructure news from approved public sources.",
        "skill_type": "automation",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "risk_level": "low",
        "permissions": {
            "network": ["reuters.com", "apnews.com"],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "codex",
        "enabled": False,
    }


def test_valid_low_risk_manifest_passes() -> None:
    manifest = validate_manifest(valid_manifest())

    assert manifest.name == "ai_news_digest"
    assert manifest.skill_type == "automation"
    assert manifest.interface_type == "chat"
    assert manifest.permissions.network == ["reuters.com", "apnews.com"]


def test_manifest_accepts_optional_display_name() -> None:
    data = valid_manifest()
    data["display_name"] = "Simple Calculator"

    manifest = validate_manifest(data)

    assert manifest.display_name == "Simple Calculator"


def test_manifest_accepts_simple_dependencies() -> None:
    data = valid_manifest()
    data["dependencies"] = ["requests", "beautifulsoup4==4.12.3"]

    manifest = validate_manifest(data)

    assert manifest.dependencies == ["requests", "beautifulsoup4==4.12.3"]


def test_manifest_rejects_url_dependencies() -> None:
    data = valid_manifest()
    data["dependencies"] = ["https://example.com/package.whl"]

    with pytest.raises(ManifestValidationError, match="dependencies"):
        validate_manifest(data)


def test_manifest_accepts_tool_interface_and_io_schemas() -> None:
    data = valid_manifest()
    data["interface_type"] = "tool"
    data["input_schema"] = {"type": "object", "properties": {"expression": {"type": "string"}}}
    data["output_schema"] = {"type": "object", "properties": {"result": {"type": "number"}}}
    data["tool_ui_schema"] = {
        "title": "Calculator",
        "fields": [{"name": "expression", "label": "Expression", "type": "text"}],
    }

    manifest = validate_manifest(data)

    assert manifest.interface_type == "tool"
    assert manifest.input_schema == {"type": "object", "properties": {"expression": {"type": "string"}}}
    assert manifest.output_schema == {"type": "object", "properties": {"result": {"type": "number"}}}
    assert manifest.tool_ui_schema["title"] == "Calculator"


def test_manifest_rejects_unknown_interface_type() -> None:
    data = valid_manifest()
    data["interface_type"] = "dashboard"

    with pytest.raises(ManifestValidationError, match="interface_type"):
        validate_manifest(data)


def test_manifest_requires_explicit_permission_fields() -> None:
    data = valid_manifest()
    del data["permissions"]["shell"]

    with pytest.raises(ManifestValidationError, match="shell"):
        validate_manifest(data)


def test_manifest_rejects_wildcard_network_access() -> None:
    data = valid_manifest()
    data["permissions"]["network"] = ["*"]

    with pytest.raises(ManifestValidationError, match="explicit domains"):
        validate_manifest(data)


def test_manifest_rejects_url_network_permissions() -> None:
    data = valid_manifest()
    data["permissions"]["network"] = ["https://example.com/feed"]

    with pytest.raises(ManifestValidationError, match="domains"):
        validate_manifest(data)


def test_manifest_rejects_parent_directory_entrypoint() -> None:
    data = valid_manifest()
    data["entrypoint"] = "../skill.py"

    with pytest.raises(ManifestValidationError, match="traverse"):
        validate_manifest(data)


def test_manifest_rejects_parent_directory_filesystem_permission() -> None:
    data = valid_manifest()
    data["permissions"]["filesystem_write"] = ["../outside"]

    with pytest.raises(ManifestValidationError, match="traverse"):
        validate_manifest(data)


def test_manifest_requires_risk_level_to_match_permissions() -> None:
    data = valid_manifest()
    data["permissions"]["filesystem_read"] = ["selected_notes"]

    with pytest.raises(ManifestValidationError, match="at least medium"):
        validate_manifest(data)


def test_manifest_treats_own_cache_read_as_low_risk() -> None:
    data = valid_manifest()
    data["permissions"]["filesystem_read"] = ["./cache"]
    data["permissions"]["filesystem_write"] = ["./cache"]

    manifest = validate_manifest(data)

    assert manifest.risk_level == "low"


def test_manifest_accepts_declared_medium_risk_for_filesystem_read() -> None:
    data = valid_manifest()
    data["risk_level"] = "medium"
    data["permissions"]["filesystem_read"] = ["selected_notes"]

    manifest = validate_manifest(data)

    assert manifest.risk_level == "medium"


def test_instruction_manifest_requires_instructions_path() -> None:
    data = valid_manifest()
    data["skill_type"] = "instruction"
    data["entrypoint"] = None
    data["permissions"] = no_permissions()

    with pytest.raises(ManifestValidationError, match="instructions_path"):
        validate_manifest(data)


def test_instruction_manifest_rejects_entrypoint() -> None:
    data = valid_manifest()
    data["skill_type"] = "instruction"
    data["instructions_path"] = "README.md"
    data["permissions"] = no_permissions()

    with pytest.raises(ManifestValidationError, match="cannot declare entrypoint"):
        validate_manifest(data)


def test_instruction_manifest_requires_no_permissions() -> None:
    data = valid_manifest()
    data["skill_type"] = "instruction"
    data["entrypoint"] = None
    data["instructions_path"] = "README.md"
    data["permissions"] = {
        "network": [],
        "filesystem_read": [],
        "filesystem_write": ["./cache"],
        "secrets": [],
        "shell": False,
    }

    with pytest.raises(ManifestValidationError, match="must request no permissions"):
        validate_manifest(data)


def test_valid_instruction_manifest_passes() -> None:
    data = valid_manifest()
    data["skill_type"] = "instruction"
    data["entrypoint"] = None
    data["instructions_path"] = "README.md"
    data["permissions"] = no_permissions()

    manifest = validate_manifest(data)

    assert manifest.skill_type == "instruction"
    assert manifest.entrypoint is None
    assert manifest.instructions_path == "README.md"


def test_automation_manifest_requires_entrypoint() -> None:
    data = valid_manifest()
    data["entrypoint"] = None

    with pytest.raises(ManifestValidationError, match="automation skills require entrypoint"):
        validate_manifest(data)


def test_automation_manifest_file_requires_tests_directory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "automation_without_tests"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps(valid_manifest()), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="automation skills require tests"):
        validate_manifest_file(skill_dir / "manifest.json")


def test_hybrid_manifest_requires_instructions_path() -> None:
    data = valid_manifest()
    data["skill_type"] = "hybrid"

    with pytest.raises(ManifestValidationError, match="hybrid skills require instructions_path"):
        validate_manifest(data)


def test_hybrid_manifest_requires_entrypoint() -> None:
    data = valid_manifest()
    data["skill_type"] = "hybrid"
    data["entrypoint"] = None
    data["instructions_path"] = "README.md"

    with pytest.raises(ManifestValidationError, match="hybrid skills require entrypoint"):
        validate_manifest(data)


def test_hybrid_manifest_file_requires_tests_directory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "hybrid_without_tests"
    skill_dir.mkdir()
    data = valid_manifest()
    data["skill_type"] = "hybrid"
    data["instructions_path"] = "README.md"
    (skill_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="hybrid skills require tests"):
        validate_manifest_file(skill_dir / "manifest.json")


def no_permissions() -> dict:
    return {
        "network": [],
        "filesystem_read": [],
        "filesystem_write": [],
        "secrets": [],
        "shell": False,
    }
