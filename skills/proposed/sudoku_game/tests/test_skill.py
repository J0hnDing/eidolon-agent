import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
SKILL_PATH = ROOT / "skill.py"

SOLVED_GRID = (
    "534678912"
    "672195348"
    "198342567"
    "859761423"
    "426853791"
    "713924856"
    "961537284"
    "287419635"
    "345286179"
)
PUZZLE = (
    "530070000"
    "600195000"
    "098000060"
    "800060003"
    "400803001"
    "700020006"
    "060000280"
    "000419005"
    "000080079"
)


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def run_skill(payload):
    process = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
    )
    assert process.returncode == 0, process.stderr
    assert process.stderr == ""
    return json.loads(process.stdout)


def assert_output_contract(result):
    assert set(result) == {
        "puzzle",
        "grid",
        "difficulty",
        "is_valid",
        "is_complete",
        "errors",
        "hint",
        "message",
    }
    assert isinstance(result["is_valid"], bool)
    assert isinstance(result["is_complete"], bool)
    assert isinstance(result["errors"], list)
    assert isinstance(result["message"], str)
    assert result["puzzle"] is None or isinstance(result["puzzle"], str)
    assert result["grid"] is None or isinstance(result["grid"], str)
    assert result["difficulty"] is None or isinstance(result["difficulty"], str)
    assert result["hint"] is None or isinstance(result["hint"], (str, dict))


def test_manifest_declares_safe_tool_contract():
    manifest = load_manifest()

    assert manifest["name"] == "sudoku_game"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["dependencies"] == []
    assert manifest["risk_level"] == "low"
    assert manifest["permissions"] == {
        "network": [],
        "filesystem_read": [],
        "filesystem_write": [],
        "secrets": [],
        "shell": False,
    }

    assert manifest["input_schema"]["required"] == ["mode"]
    assert manifest["input_schema"]["additionalProperties"] is False
    assert manifest["output_schema"]["additionalProperties"] is False
    assert set(manifest["output_schema"]["required"]) == {
        "is_valid",
        "is_complete",
        "errors",
        "message",
    }

    ui_schema = manifest["tool_ui_schema"]
    field_names = {field["name"] for field in ui_schema["fields"]}
    assert {"mode", "difficulty", "puzzle", "solution", "include_hint"} <= field_names
    assert ui_schema["result_template"]["primary_field"] == "message"


def test_generate_mode_returns_playable_puzzle_and_optional_hint():
    result = run_skill({"mode": "generate", "difficulty": "hard", "include_hint": True})

    assert_output_contract(result)
    assert result["is_valid"] is True
    assert result["is_complete"] is False
    assert result["errors"] == []
    assert result["difficulty"] == "hard"
    assert result["message"] == "Generated a hard Sudoku puzzle."
    assert len(result["puzzle"]) == 81
    assert set(result["puzzle"]) <= set(".123456789")
    assert result["puzzle"].count(".") == 52
    assert result["grid"].count("\n") == 8
    assert all(len(row) == 9 for row in result["grid"].splitlines())
    assert result["hint"] == {
        "row": 1,
        "column": 1,
        "value": "5",
        "message": "Try 5 at row 1, column 1.",
    }


def test_check_mode_accepts_valid_completed_solution():
    result = run_skill(
        {
            "mode": "check",
            "puzzle": PUZZLE,
            "solution": SOLVED_GRID,
            "include_hint": True,
        }
    )

    assert_output_contract(result)
    assert result["puzzle"].count(".") == PUZZLE.count("0")
    assert result["grid"] == "\n".join(
        SOLVED_GRID[index : index + 9] for index in range(0, 81, 9)
    )
    assert result["is_valid"] is True
    assert result["is_complete"] is True
    assert result["errors"] == []
    assert result["hint"] == {
        "row": 1,
        "column": 3,
        "value": "4",
        "message": "Try 4 at row 1, column 3.",
    }
    assert result["message"] == "Solution is complete and valid."


def test_check_mode_rejects_incorrect_and_rule_breaking_solution():
    bad_solution = "1" + SOLVED_GRID[1:]
    result = run_skill({"mode": "check", "puzzle": PUZZLE, "solution": bad_solution})

    assert_output_contract(result)
    assert result["is_valid"] is False
    assert result["is_complete"] is True
    assert any("duplicate 1 in row" in error for error in result["errors"])
    assert any("incorrect entries" in error for error in result["errors"])
    assert result["message"] == "Solution is not complete or contains errors."


def test_malformed_input_reports_validation_errors_without_crashing():
    result = run_skill({"mode": "check", "puzzle": "123", "solution": "x" * 81})

    assert_output_contract(result)
    assert result["is_valid"] is False
    assert result["is_complete"] is False
    assert "puzzle must contain exactly 81 cells" in result["errors"]
    assert "solution contains invalid characters: x" in result["errors"]
    assert result["message"] == "The submitted Sudoku input is invalid."


def test_unsupported_mode_and_non_object_input_follow_json_contract():
    unsupported = run_skill({"mode": "solve"})
    assert_output_contract(unsupported)
    assert unsupported["is_valid"] is False
    assert unsupported["errors"] == ["mode must be generate or check"]

    process = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps(["not", "an", "object"]),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
    )
    assert process.returncode == 0
    non_object = json.loads(process.stdout)
    assert_output_contract(non_object)
    assert non_object["errors"] == ["input must be a JSON object"]
