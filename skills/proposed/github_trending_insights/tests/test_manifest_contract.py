import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_manifest():
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def collect_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from collect_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from collect_strings(item)


def test_manifest_declares_tool_identity_permissions_and_dependencies():
    manifest = load_manifest()

    assert manifest["name"] == "github_trending_insights"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["instructions_path"] == "SKILL.md"
    assert manifest["schedule"] is None

    assert set(manifest["dependencies"]) == {"requests", "beautifulsoup4"}

    permissions = manifest["permissions"]
    assert permissions["network"] == ["github.com"]
    assert permissions["filesystem_read"] == ["./cache"]
    assert permissions["filesystem_write"] == ["./cache"]
    assert permissions["secrets"] == []
    assert permissions["shell"] is False
    assert permissions["codex"] == {
        "call_response": True,
        "internet_access": False,
    }


def test_input_schema_contains_expected_fields_and_constraints():
    manifest = load_manifest()
    input_schema = manifest["input_schema"]
    properties = input_schema["properties"]

    assert input_schema["type"] == "object"
    assert set(properties) == {
        "language",
        "time_range",
        "max_projects",
        "focus",
        "include_risks",
    }
    assert set(input_schema["required"]) == {"time_range", "max_projects"}

    assert properties["language"]["type"] == "string"
    assert properties["time_range"]["type"] == "string"
    assert properties["time_range"]["enum"] == ["daily", "weekly", "monthly"]
    assert properties["max_projects"]["type"] == "integer"
    assert properties["max_projects"]["minimum"] == 1
    assert properties["max_projects"]["maximum"] == 25
    assert properties["focus"]["type"] == "string"
    assert properties["include_risks"]["type"] == "boolean"


def test_output_schema_requires_report_shape_with_optional_risks():
    manifest = load_manifest()
    output_schema = manifest["output_schema"]
    properties = output_schema["properties"]

    assert output_schema["type"] == "object"
    assert set(output_schema["required"]) == {
        "summary",
        "projects",
        "themes",
        "recommendations",
    }
    assert "risks" in properties
    assert "risks" not in output_schema["required"]

    assert properties["summary"]["type"] == "string"
    assert properties["themes"] == {"type": "array", "items": {"type": "string"}}
    assert properties["recommendations"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert properties["risks"] == {"type": "array", "items": {"type": "string"}}

    project_schema = properties["projects"]["items"]
    assert properties["projects"]["type"] == "array"
    assert set(project_schema["required"]) == {
        "name",
        "url",
        "description",
        "trend_signal",
        "insights",
    }
    assert {"language", "stars"}.issubset(project_schema["properties"])


def test_tool_ui_schema_is_declarative_and_complete_for_tools_page():
    manifest = load_manifest()
    ui_schema = manifest["tool_ui_schema"]
    fields = {field["name"]: field for field in ui_schema["fields"]}

    assert ui_schema["title"] == "GitHub Trending Insights"
    assert ui_schema["submit_label"] == "Analyze"
    assert set(fields) == {
        "language",
        "time_range",
        "max_projects",
        "focus",
        "include_risks",
    }

    assert fields["language"]["type"] == "text"
    assert fields["language"]["required"] is False
    assert fields["language"]["default"] == ""
    assert fields["time_range"]["type"] == "select"
    assert fields["time_range"]["required"] is True
    assert fields["time_range"]["default"] == "daily"
    assert fields["time_range"]["options"] == ["daily", "weekly", "monthly"]
    assert fields["max_projects"]["type"] == "number"
    assert fields["max_projects"]["required"] is True
    assert fields["max_projects"]["default"] == 10
    assert fields["focus"]["type"] == "textarea"
    assert fields["include_risks"]["type"] == "checkbox"
    assert fields["include_risks"]["default"] is True

    for field in fields.values():
        assert field["label"]
        assert "required" in field
        assert "default" in field
        assert field["help_text"]

    assert ui_schema["result_template"] == {
        "primary_field": "summary",
        "primary_label": "Summary",
    }

    declarative_text = " ".join(collect_strings(ui_schema)).lower()
    for forbidden in ("react", "html", "javascript", ".jsx", ".tsx"):
        assert forbidden not in declarative_text


def test_readme_and_skill_instructions_describe_boundaries_without_approval_claims():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    skill_doc = (ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
    combined = f"{readme}\n{skill_doc}"

    for phrase in (
        "local-first",
        "github.com",
        "./cache",
        "shell access is disabled",
        "secrets are not requested",
        "call_response",
        "internet_access",
        "tool_ui_schema",
    ):
        assert phrase in combined

    assert "not installed, enabled, scheduled, or approved" in readme
    assert "runtime approval is still controlled by the platform" in readme
    assert "does not include react, html, javascript, or frontend application source code" in readme
    assert "write a single json object to stdout" in skill_doc
