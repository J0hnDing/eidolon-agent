import json
from pathlib import Path

import pytest

from app.schemas.manifest import manifest_permission_requests
from app.services.manifest_validator import (
    ManifestValidationError,
    classify_permission_risk,
    validate_manifest,
    validate_manifest_file,
)


def valid_manifest() -> dict:
    return {
        "manifest_version": 1,
        "name": "ai_news_digest",
        "description": "Summarizes AI infrastructure news from approved public sources.",
        "runtime": "function",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "permissions": {
            "network": ["reuters.com", "apnews.com"],
        },
        "schedule": None,
    }


def test_valid_low_risk_manifest_passes() -> None:
    manifest = validate_manifest(valid_manifest())

    assert manifest.name == "ai_news_digest"
    assert manifest.runtime == "function"
    assert manifest.permissions.network == ["reuters.com", "apnews.com"]
    assert manifest.permissions.codex.call_response is False
    assert manifest.permissions.codex.internet_access is False
    assert manifest.permissions.filesystem_read == ["./cache"]
    assert manifest.permissions.filesystem_write == ["./cache"]


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


def test_manifest_accepts_io_schemas() -> None:
    data = valid_manifest()
    data["input_schema"] = {"type": "object", "properties": {"expression": {"type": "string"}}}
    data["output_schema"] = {"type": "object", "properties": {"result": {"type": "number"}}}

    manifest = validate_manifest(data)

    assert manifest.input_schema == {"type": "object", "properties": {"expression": {"type": "string"}}}
    assert manifest.output_schema == {"type": "object", "properties": {"result": {"type": "number"}}}


def test_manifest_validates_json_schemas_and_function_requirements() -> None:
    data = valid_manifest()
    data["input_schema"] = {"type": "object"}
    data["output_schema"] = {"type": "object"}
    data["function_requirements"] = ["normalize_text"]

    manifest = validate_manifest(data)

    assert manifest.function_requirements[0] == "normalize_text"


def test_manifest_rejects_invalid_or_non_object_function_schema() -> None:
    invalid = valid_manifest()
    invalid["input_schema"] = {"type": "not-a-json-schema-type"}
    with pytest.raises(ManifestValidationError, match="invalid JSON Schema"):
        validate_manifest(invalid)

    non_object = valid_manifest()
    non_object["input_schema"] = {"type": "array"}
    with pytest.raises(ManifestValidationError, match="type object"):
        validate_manifest(non_object)


def test_manifest_rejects_duplicate_and_self_function_requirements() -> None:
    duplicate = valid_manifest()
    duplicate["function_requirements"] = ["normalize_text", "normalize_text"]
    with pytest.raises(ManifestValidationError, match="duplicate"):
        validate_manifest(duplicate)

    self_reference = valid_manifest()
    self_reference["function_requirements"] = [self_reference["name"]]
    with pytest.raises(ManifestValidationError, match="cannot require itself"):
        validate_manifest(self_reference)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interface_type", "tool"),
        ("tool_ui_schema", {"title": "Calculator", "fields": []}),
    ],
)
def test_manifest_rejects_removed_interface_fields(field: str, value: object) -> None:
    data = valid_manifest()
    data[field] = value

    with pytest.raises(ManifestValidationError, match=field):
        validate_manifest(data)


def test_manifest_accepts_approval_only_permissions_and_explicit_false() -> None:
    data = valid_manifest()
    data["permissions"] = {"codex": {"call_response": False}}

    manifest = validate_manifest(data)

    assert manifest.permissions.codex.call_response is False
    assert manifest.permissions.filesystem_read == ["./cache"]
    assert manifest_permission_requests(manifest.permissions) == {}


def test_manifest_permission_contract_contains_only_approval_requests() -> None:
    data = valid_manifest()
    data["permissions"] = {
        "network": ["example.com"],
        "codex": {"call_response": True, "internet_access": False},
    }

    manifest = validate_manifest(data)

    assert manifest_permission_requests(manifest.permissions) == {
        "network": ["example.com"],
        "codex": {"call_response": True},
    }


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


def test_manifest_accepts_codex_internet_permission() -> None:
    data = valid_manifest()
    data["permissions"]["codex"] = {"call_response": True, "internet_access": True}

    manifest = validate_manifest(data)

    assert manifest.permissions.codex.call_response is True
    assert manifest.permissions.codex.internet_access is True


def test_manifest_rejects_unknown_codex_permission() -> None:
    data = valid_manifest()
    data["permissions"]["codex"] = {"call_response": True, "filesystem_read": True}

    with pytest.raises(ManifestValidationError, match="filesystem_read"):
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


def test_manifest_risk_is_derived_from_permissions() -> None:
    data = valid_manifest()
    data["permissions"]["filesystem_read"] = ["selected_notes"]

    manifest = validate_manifest(data)

    assert classify_permission_risk(manifest.permissions) == "medium"


def test_manifest_treats_own_cache_read_as_low_risk() -> None:
    data = valid_manifest()
    data["permissions"]["network"] = []
    data["permissions"]["filesystem_read"] = ["./cache"]
    data["permissions"]["filesystem_write"] = ["./cache"]

    manifest = validate_manifest(data)

    assert classify_permission_risk(manifest.permissions) == "low"


def test_manifest_network_and_dependencies_are_deterministically_medium_risk() -> None:
    data = valid_manifest()
    data["permissions"]["network"] = ["example.com"]
    manifest = validate_manifest(data)
    assert classify_permission_risk(manifest.permissions, manifest.dependencies) == "medium"

    dependency_manifest = validate_manifest({**valid_manifest(), "dependencies": ["feedparser"]})
    assert classify_permission_risk(
        dependency_manifest.permissions,
        dependency_manifest.dependencies,
    ) == "medium"


def test_manifest_legacy_backend_state_is_not_part_of_canonical_output() -> None:
    data = valid_manifest()
    data["risk_level"] = "medium"
    data["created_by"] = "legacy"
    data["enabled"] = True

    manifest = validate_manifest(data)

    assert set(manifest.model_dump()) >= {"manifest_version", "runtime", "entrypoint", "permissions"}
    assert not {"risk_level", "created_by", "enabled"} & set(manifest.model_dump())


def test_manifest_requires_entrypoint() -> None:
    data = valid_manifest()
    data["entrypoint"] = None

    with pytest.raises(ManifestValidationError, match="entrypoint"):
        validate_manifest(data)


def test_web_app_manifest_requires_importable_asgi_entrypoint() -> None:
    data = valid_manifest()
    data["runtime"] = "web_app"
    data["entrypoint"] = "app.py"

    with pytest.raises(ManifestValidationError, match="module:attribute"):
        validate_manifest(data)


def test_web_app_manifest_rejects_bounded_run_schedule() -> None:
    data = valid_manifest()
    data["runtime"] = "web_app"
    data["entrypoint"] = "app:app"
    data["schedule"] = {
        "type": "daily",
        "time": "09:00",
        "timezone": "America/Toronto",
        "input": {},
    }

    with pytest.raises(ManifestValidationError, match="web_app skills cannot declare schedules"):
        validate_manifest(data)


def test_function_manifest_rejects_schedule() -> None:
    data = valid_manifest()
    data["schedule"] = {
        "type": "daily",
        "time": "09:00",
        "timezone": "America/Toronto",
        "input": {},
    }

    with pytest.raises(ManifestValidationError, match="function skills cannot declare schedules"):
        validate_manifest(data)


def test_service_manifest_requires_object_schemas_and_schedule() -> None:
    data = valid_manifest()
    data["runtime"] = "service"
    data["input_schema"] = {"type": "object", "additionalProperties": False}
    data["output_schema"] = {"type": "object", "additionalProperties": False}
    data["schedule"] = {
        "type": "daily",
        "time": "09:00",
        "timezone": "America/Toronto",
        "input": {},
    }

    manifest = validate_manifest(data)

    assert manifest.runtime == "service"
    assert manifest.schedule is not None

    data["schedule"] = None
    with pytest.raises(ManifestValidationError, match="service skills require a schedule"):
        validate_manifest(data)


def test_web_app_manifest_file_resolves_module_entrypoint(tmp_path: Path) -> None:
    skill_dir = tmp_path / "web_app"
    skill_dir.mkdir()
    (skill_dir / "tests").mkdir()
    data = valid_manifest()
    data["runtime"] = "web_app"
    data["entrypoint"] = "app:app"
    (skill_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    (skill_dir / "app.py").write_text("async def app(scope, receive, send):\n    pass\n", encoding="utf-8")

    manifest = validate_manifest_file(skill_dir / "manifest.json")

    assert manifest.runtime == "web_app"


def test_manifest_allows_optional_instructions_path() -> None:
    data = valid_manifest()
    data["instructions_path"] = "SKILL.md"

    manifest = validate_manifest(data)

    assert manifest.instructions_path == "SKILL.md"


def test_manifest_file_requires_tests_directory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill_without_tests"
    skill_dir.mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps(valid_manifest()), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="skills require tests"):
        validate_manifest_file(skill_dir / "manifest.json")


def test_manifest_file_requires_declared_entrypoint(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill_without_entrypoint_file"
    skill_dir.mkdir()
    (skill_dir / "tests").mkdir()
    (skill_dir / "manifest.json").write_text(json.dumps(valid_manifest()), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="Declared file is missing: skill.py"):
        validate_manifest_file(skill_dir / "manifest.json")


def test_manifest_file_requires_declared_instructions_file(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill_without_instructions_file"
    skill_dir.mkdir()
    (skill_dir / "tests").mkdir()
    data = valid_manifest()
    data["instructions_path"] = "SKILL.md"
    (skill_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{}')\n", encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="Declared file is missing: SKILL.md"):
        validate_manifest_file(skill_dir / "manifest.json")


def test_manifest_file_accepts_declared_instructions_file(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill_with_instructions"
    skill_dir.mkdir()
    (skill_dir / "tests").mkdir()
    data = valid_manifest()
    data["instructions_path"] = "SKILL.md"
    (skill_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{}')\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("Instructions\n", encoding="utf-8")
    (skill_dir / "tests" / "test_skill.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    manifest = validate_manifest_file(skill_dir / "manifest.json")

    assert manifest.instructions_path == "SKILL.md"


def test_manifest_rejects_removed_skill_type_field() -> None:
    data = valid_manifest()
    data["skill_type"] = "automation"

    with pytest.raises(ManifestValidationError, match="skill_type"):
        validate_manifest(data)
