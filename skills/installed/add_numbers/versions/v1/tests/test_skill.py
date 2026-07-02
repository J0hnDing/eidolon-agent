import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
SKILL_PATH = ROOT / "skill.py"


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def run_skill(payload):
    return subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )


def parse_stdout(completed):
    assert completed.stdout.strip(), "skill.py should emit JSON on stdout"
    return json.loads(completed.stdout)


def test_manifest_declares_low_risk_tool_contract():
    manifest = load_manifest()

    assert manifest["name"] == "add_numbers"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["dependencies"] == []
    assert manifest["risk_level"] == "low"
    assert manifest["schedule"] is None
    assert manifest["enabled"] is False

    permissions = manifest["permissions"]
    assert permissions["network"] == []
    assert permissions["filesystem_read"] == []
    assert permissions["filesystem_write"] == []
    assert permissions["secrets"] == []
    assert permissions["shell"] is False


def test_manifest_schemas_require_only_numeric_inputs_and_result_output():
    manifest = load_manifest()

    input_schema = manifest["input_schema"]
    assert input_schema["type"] == "object"
    assert set(input_schema["required"]) == {"a", "b"}
    assert input_schema["additionalProperties"] is False
    assert input_schema["properties"]["a"]["type"] == "number"
    assert input_schema["properties"]["b"]["type"] == "number"

    output_schema = manifest["output_schema"]
    assert output_schema["type"] == "object"
    assert output_schema["required"] == ["result"]
    assert output_schema["additionalProperties"] is False
    assert output_schema["properties"]["result"]["type"] == "number"


def test_tool_ui_schema_is_declarative_for_two_number_fields_and_result():
    manifest = load_manifest()
    tool_ui_schema = manifest["tool_ui_schema"]

    assert isinstance(tool_ui_schema, dict)
    assert tool_ui_schema["title"]
    assert tool_ui_schema["submit_label"]

    fields = tool_ui_schema["fields"]
    assert isinstance(fields, list)
    assert len(fields) == 2
    assert {field["name"] for field in fields} == {"a", "b"}
    for field in fields:
        assert field["type"] == "number"
        assert field["required"] is True
        assert "label" in field

    assert tool_ui_schema["result_template"]["primary_field"] == "result"


def test_skill_accepts_json_stdin_and_returns_integer_sum():
    completed = run_skill({"a": 12, "b": 30})

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert parse_stdout(completed) == {"result": 42}


def test_skill_handles_decimal_inputs():
    completed = run_skill({"a": 1.25, "b": 2.5})

    assert completed.returncode == 0
    assert completed.stderr == ""
    output = parse_stdout(completed)
    assert output == {"result": 3.75}


def test_skill_rejects_extra_fields_and_non_numeric_booleans():
    extra_field = run_skill({"a": 1, "b": 2, "c": 3})
    assert extra_field.returncode != 0
    extra_output = parse_stdout(extra_field)
    assert "error" in extra_output
    assert "unexpected field" in extra_output["error"]

    boolean_input = run_skill({"a": True, "b": 2})
    assert boolean_input.returncode != 0
    boolean_output = parse_stdout(boolean_input)
    assert "error" in boolean_output
    assert "must be a number" in boolean_output["error"]
