import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"


def load_manifest():
    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def test_manifest_exposes_weather_outfit_planner_as_automation_tool():
    manifest = load_manifest()
    tool_ui_schema = manifest.get("tool_ui_schema")

    assert manifest["name"] == "weather_outfit_planner"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert isinstance(tool_ui_schema, dict)
    assert tool_ui_schema["title"] == "Weather Outfit Planner"
    assert tool_ui_schema["submit_label"] == "Plan Outfit"
    assert isinstance(tool_ui_schema.get("description"), str)
    assert tool_ui_schema["description"].strip()


def test_tool_ui_fields_are_declarative_and_match_input_schema_names():
    manifest = load_manifest()
    input_schema = manifest["input_schema"]
    fields = manifest["tool_ui_schema"].get("fields")

    assert input_schema["required"] == ["city", "units"]
    assert input_schema["additionalProperties"] is False
    assert isinstance(fields, list)
    assert [field["name"] for field in fields] == ["city", "units"]
    assert {field["name"] for field in fields} == set(input_schema["properties"])

    for field in fields:
        assert field["required"] is True
        assert isinstance(field.get("label"), str) and field["label"].strip()
        assert isinstance(field.get("help_text"), str) and field["help_text"].strip()
        assert isinstance(field.get("type"), str) and field["type"].strip()
        assert "component" not in field
        assert "render" not in field
        assert "script" not in field


def test_city_text_field_has_required_labeling_and_defaults():
    manifest = load_manifest()
    fields = {field["name"]: field for field in manifest["tool_ui_schema"]["fields"]}
    city_schema = manifest["input_schema"]["properties"]["city"]
    city_field = fields["city"]

    assert city_schema["type"] == "string"
    assert city_schema["minLength"] == 1
    assert city_schema["maxLength"] == 120
    assert city_field["type"] == "text"
    assert city_field["label"] == "City"
    assert isinstance(city_field.get("placeholder"), str)
    assert city_field["placeholder"].strip()
    assert isinstance(city_field.get("help_text"), str)
    assert city_field["help_text"].strip()
    assert city_field["default"] == ""


def test_units_select_field_options_and_default_match_input_schema():
    manifest = load_manifest()
    fields = {field["name"]: field for field in manifest["tool_ui_schema"]["fields"]}
    units_schema = manifest["input_schema"]["properties"]["units"]
    units_field = fields["units"]

    assert units_schema["type"] == "string"
    assert units_schema["enum"] == ["metric", "imperial"]
    assert units_field["type"] == "select"
    assert units_field["label"] == "Units"
    assert units_field["default"] == "metric"
    assert units_field["options"] == units_schema["enum"]


def test_result_template_renders_all_output_fields_with_recommendation_primary():
    manifest = load_manifest()
    output_schema = manifest["output_schema"]
    result_template = manifest["tool_ui_schema"].get("result_template")

    assert isinstance(result_template, dict)
    assert result_template["primary_field"] == "recommendation"
    assert isinstance(result_template.get("primary_label"), str)
    assert result_template["primary_label"].strip()

    secondary_fields = result_template.get("secondary_fields")
    assert isinstance(secondary_fields, list)
    secondary_names = [item["field"] for item in secondary_fields]
    assert secondary_names == [
        "commute_note",
        "temperature",
        "condition",
        "umbrella",
        "jacket",
    ]

    output_fields = set(output_schema["properties"])
    visible_fields = {result_template["primary_field"], *secondary_names}
    assert visible_fields == output_fields
    assert set(output_schema["required"]) == output_fields

    for item in secondary_fields:
        assert isinstance(item.get("label"), str)
        assert item["label"].strip()


def test_tool_ui_schema_is_manifest_only_without_custom_frontend_artifacts():
    manifest = load_manifest()
    encoded_schema = json.dumps(manifest["tool_ui_schema"]).lower()

    blocked_terms = [
        "react",
        "javascript",
        "component",
        "tsx",
        "jsx",
    ]
    assert not any(term in encoded_schema for term in blocked_terms)

    def walk_values(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from walk_values(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk_values(child)
        elif isinstance(value, str):
            yield value.lower()

    string_values = list(walk_values(manifest["tool_ui_schema"]))
    assert not any("<script" in value for value in string_values)
    assert not any("<html" in value for value in string_values)

    unexpected_frontend_paths = [
        ROOT / "src",
        ROOT / "frontend",
        ROOT / "components",
        ROOT / "tool_ui.tsx",
        ROOT / "tool_ui.jsx",
        ROOT / "tool_ui.js",
        ROOT / "tool_ui.html",
    ]
    assert not any(path.exists() for path in unexpected_frontend_paths)
