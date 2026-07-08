import json
import re
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = SKILL_DIR / "manifest.json"


def load_manifest():
    with MANIFEST_PATH.open(encoding="utf-8") as manifest_file:
        return json.load(manifest_file)


def test_manifest_declares_wordle_automation_tool_contract():
    manifest = load_manifest()

    assert manifest["name"] == "wordle_game"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["risk_level"] == "low"


def test_runtime_permissions_are_limited_to_local_cache_only():
    manifest = load_manifest()

    assert manifest["permissions"] == {
        "network": [],
        "filesystem_read": [],
        "filesystem_write": ["./cache"],
        "secrets": [],
        "shell": False,
    }
    assert manifest.get("dependencies", []) == []
    assert manifest.get("external_dependencies", []) == []


def test_input_schema_requires_exactly_five_alpha_guess_and_optional_new_game():
    schema = load_manifest()["input_schema"]

    assert schema["type"] == "object"
    assert schema["required"] == ["guess"]
    assert schema["additionalProperties"] is False

    guess = schema["properties"]["guess"]
    assert guess["type"] == "string"
    assert guess["minLength"] == 5
    assert guess["maxLength"] == 5
    assert guess["pattern"] == "^[A-Za-z]{5}$"
    assert re.fullmatch(guess["pattern"], "CRANE")
    assert re.fullmatch(guess["pattern"], "crane")
    assert not re.fullmatch(guess["pattern"], "CR4NE")
    assert not re.fullmatch(guess["pattern"], "TRAINS")

    new_game = schema["properties"]["new_game"]
    assert new_game["type"] == "boolean"
    assert new_game["default"] is False


def test_output_schema_matches_wordle_result_contract():
    schema = load_manifest()["output_schema"]

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "status",
        "result",
        "attempts_used",
        "attempts_remaining",
        "feedback",
        "history",
    ]

    properties = schema["properties"]
    assert properties["status"]["enum"] == [
        "in_progress",
        "won",
        "lost",
        "reset",
        "error",
    ]
    assert properties["result"]["type"] == "string"
    assert properties["attempts_used"]["type"] == "integer"
    assert properties["attempts_used"]["minimum"] == 0
    assert properties["attempts_used"]["maximum"] == 6
    assert properties["attempts_remaining"]["type"] == "integer"
    assert properties["attempts_remaining"]["minimum"] == 0
    assert properties["attempts_remaining"]["maximum"] == 6

    feedback_item = properties["feedback"]["items"]
    assert feedback_item["required"] == ["letter", "state"]
    assert feedback_item["additionalProperties"] is False
    assert feedback_item["properties"]["letter"]["type"] == "string"
    assert feedback_item["properties"]["state"]["enum"] == [
        "correct",
        "present",
        "absent",
    ]
    assert properties["history"]["type"] == "array"


def test_tool_ui_schema_is_declarative_and_renderable_by_tools_page():
    ui_schema = load_manifest()["tool_ui_schema"]

    assert ui_schema["title"] == "Wordle Game"
    assert ui_schema["submit_label"] == "Submit Guess"
    assert ui_schema["result_template"] == {
        "primary_field": "result",
        "primary_label": "Result",
    }

    fields_by_name = {field["name"]: field for field in ui_schema["fields"]}
    assert set(fields_by_name) == {"guess", "new_game"}
    assert fields_by_name["guess"] == {
        "name": "guess",
        "label": "Guess",
        "type": "text",
        "placeholder": "CRANE",
        "help_text": "Enter exactly five letters.",
        "required": True,
        "default": "",
    }
    assert fields_by_name["new_game"] == {
        "name": "new_game",
        "label": "Start New Game",
        "type": "checkbox",
        "placeholder": "",
        "help_text": "Reset the current saved Wordle game before applying this guess.",
        "required": False,
        "default": False,
    }


def test_manifest_task_does_not_generate_frontend_source_files():
    forbidden_suffixes = {".html", ".js", ".jsx", ".ts", ".tsx", ".css"}
    generated_frontend_files = [
        path.relative_to(SKILL_DIR)
        for path in SKILL_DIR.rglob("*")
        if path.is_file()
        and "tests" not in path.relative_to(SKILL_DIR).parts
        and path.suffix.lower() in forbidden_suffixes
    ]

    assert generated_frontend_files == []
