import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
SKILL_PATH = ROOT / "skill.py"


def run_skill(payload):
    completed = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def test_manifest_declares_safe_tool_automation_contract():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["name"] == "simple_wordle_game"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["risk_level"] == "low"
    assert manifest["permissions"] == {
        "network": [],
        "filesystem_read": [],
        "filesystem_write": [],
        "secrets": [],
        "shell": False,
    }

    ui_schema = manifest["tool_ui_schema"]
    assert ui_schema["title"]
    assert ui_schema["submit_label"]
    assert isinstance(ui_schema["fields"], list)
    assert ui_schema["fields"]
    guesses_field = next(field for field in ui_schema["fields"] if field["name"] == "guesses")
    assert guesses_field["type"] == "textarea"
    assert guesses_field["required"] is True


def test_json_stdin_stdout_contract_and_representative_feedback():
    result = run_skill({"guesses": ["crane", "slate"]})

    assert result == {
        "status": "in_progress",
        "attempts_used": 2,
        "max_attempts": 6,
        "rows": [
            {
                "guess": "crane",
                "feedback": ["absent", "present", "absent", "correct", "absent"],
            },
            {
                "guess": "slate",
                "feedback": ["absent", "absent", "absent", "present", "absent"],
            },
        ],
        "message": "Keep guessing.",
    }


def test_winning_guess_stops_game_and_reports_success():
    result = run_skill({"guesses": ["crane", "burnt", "slate"]})

    assert result["status"] == "won"
    assert result["attempts_used"] == 2
    assert result["message"] == "You solved it."
    assert result["rows"][-1] == {
        "guess": "burnt",
        "feedback": ["correct", "correct", "correct", "correct", "correct"],
    }


def test_losing_after_six_valid_guesses_reports_answer():
    result = run_skill({"guesses": ["crane", "slate", "flint", "proud", "ghost", "spore"]})

    assert result["status"] == "lost"
    assert result["attempts_used"] == 6
    assert result["max_attempts"] == 6
    assert len(result["rows"]) == 6
    assert result["message"] == "No guesses left. The answer was burnt."


def test_invalid_guess_returns_json_error_without_traceback():
    result = run_skill({"guesses": ["crane", "zzzzz"]})

    assert result["status"] == "invalid"
    assert result["attempts_used"] == 0
    assert result["max_attempts"] == 6
    assert result["rows"] == []
    assert "embedded word list" in result["message"]

