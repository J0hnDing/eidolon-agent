import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_declares_weather_tool_contract():
    manifest = load_manifest()

    assert manifest["name"] == "weather_outfit_planner"
    assert manifest["display_name"] == "Weather Outfit Planner"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest.get("enabled") is False


def test_manifest_declares_only_requests_dependency_and_wttr_network_permission():
    manifest = load_manifest()

    assert manifest.get("dependencies") == ["requests"]

    permissions = manifest.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("network") == ["wttr.in"]
    assert permissions.get("filesystem_read") == []
    assert permissions.get("filesystem_write") == []
    assert permissions.get("secrets") == []
    assert permissions.get("shell") is False

    unsupported_permission_names = {
        "browser",
        "browser_automation",
        "email",
        "calendar",
        "finance",
        "public_posting",
        "purchases",
        "trading",
        "file_deletion",
        "delete_files",
    }
    assert unsupported_permission_names.isdisjoint(permissions)


def test_input_schema_requires_city_and_units_with_strict_constraints():
    manifest = load_manifest()
    schema = manifest["input_schema"]

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"city", "units"}

    city = schema["properties"]["city"]
    assert city["type"] == "string"
    assert city["minLength"] == 1
    assert city["maxLength"] == 120

    units = schema["properties"]["units"]
    assert units["type"] == "string"
    assert units["enum"] == ["metric", "imperial"]


def test_output_schema_requires_stable_result_fields_and_types():
    manifest = load_manifest()
    schema = manifest["output_schema"]

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False

    expected_types = {
        "recommendation": "string",
        "umbrella": "boolean",
        "jacket": "boolean",
        "temperature": "number",
        "condition": "string",
        "commute_note": "string",
    }
    assert set(schema["required"]) == set(expected_types)
    assert {
        name: details["type"]
        for name, details in schema["properties"].items()
        if name in expected_types
    } == expected_types


def test_tool_ui_schema_is_declarative_and_aligned_with_io_schemas():
    manifest = load_manifest()
    ui_schema = manifest["tool_ui_schema"]
    input_fields = set(manifest["input_schema"]["properties"])
    output_fields = set(manifest["output_schema"]["properties"])

    assert ui_schema["title"] == "Weather Outfit Planner"
    assert ui_schema["submit_label"] == "Plan Outfit"
    assert isinstance(ui_schema["description"], str)
    assert ui_schema["description"]

    fields = ui_schema["fields"]
    assert [field["name"] for field in fields] == ["city", "units"]
    assert {field["name"] for field in fields} == input_fields

    city_field = fields[0]
    assert city_field["type"] == "text"
    assert city_field["required"] is True
    assert city_field["label"]
    assert city_field["placeholder"]
    assert city_field["help_text"]
    assert "default" in city_field

    units_field = fields[1]
    assert units_field["type"] == "select"
    assert units_field["required"] is True
    assert units_field["default"] == "metric"
    assert units_field["options"] == ["metric", "imperial"]
    assert units_field["label"]
    assert units_field["help_text"]

    result_template = ui_schema["result_template"]
    assert result_template["primary_field"] == "recommendation"
    assert result_template["primary_field"] in output_fields
    secondary_fields = {item["field"] for item in result_template["secondary_fields"]}
    assert secondary_fields == output_fields - {"recommendation"}
    assert all(item["label"] for item in result_template["secondary_fields"])
