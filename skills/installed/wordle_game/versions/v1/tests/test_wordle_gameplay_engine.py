import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skill.py"
MANIFEST_PATH = ROOT / "manifest.json"
REQUIRED_OUTPUT_KEYS = {
    "status",
    "result",
    "attempts_used",
    "attempts_remaining",
    "feedback",
    "history",
}
FEEDBACK_STATES = {"correct", "present", "absent"}


def load_skill_module():
    spec = importlib.util.spec_from_file_location("wordle_skill_under_test", SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def assert_output_contract(response):
    assert set(response) == REQUIRED_OUTPUT_KEYS
    assert response["status"] in {"in_progress", "won", "lost", "reset", "error"}
    assert isinstance(response["result"], str)
    assert isinstance(response["attempts_used"], int)
    assert isinstance(response["attempts_remaining"], int)
    assert 0 <= response["attempts_used"] <= 6
    assert 0 <= response["attempts_remaining"] <= 6
    assert isinstance(response["feedback"], list)
    assert isinstance(response["history"], list)
    for item in response["feedback"]:
        assert set(item) == {"letter", "state"}
        assert item["letter"].isalpha()
        assert item["letter"] == item["letter"].upper()
        assert item["state"] in FEEDBACK_STATES


def test_manifest_declares_tool_contract_and_limited_permissions():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["name"] == "wordle_game"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest.get("dependencies", []) == []
    assert manifest["permissions"] == {
        "network": [],
        "filesystem_read": [],
        "filesystem_write": ["./cache"],
        "secrets": [],
        "shell": False,
    }

    input_schema = manifest["input_schema"]
    assert input_schema["required"] == ["guess"]
    assert input_schema["additionalProperties"] is False
    assert input_schema["properties"]["guess"]["minLength"] == 5
    assert input_schema["properties"]["guess"]["maxLength"] == 5
    assert input_schema["properties"]["guess"]["pattern"] == "^[A-Za-z]{5}$"
    assert input_schema["properties"]["new_game"]["type"] == "boolean"
    assert input_schema["properties"]["new_game"]["default"] is False

    output_schema = manifest["output_schema"]
    assert set(output_schema["required"]) == REQUIRED_OUTPUT_KEYS
    assert output_schema["properties"]["status"]["enum"] == [
        "in_progress",
        "won",
        "lost",
        "reset",
        "error",
    ]
    assert output_schema["properties"]["feedback"]["items"]["properties"]["state"]["enum"] == [
        "correct",
        "present",
        "absent",
    ]

    tool_ui_schema = manifest["tool_ui_schema"]
    fields = {field["name"]: field for field in tool_ui_schema["fields"]}
    assert tool_ui_schema["title"] == "Wordle Game"
    assert tool_ui_schema["submit_label"] == "Submit Guess"
    assert fields["guess"]["label"] == "Guess"
    assert fields["guess"]["type"] == "text"
    assert fields["guess"]["placeholder"] == "CRANE"
    assert fields["guess"]["required"] is True
    assert fields["new_game"]["label"] == "Start New Game"
    assert fields["new_game"]["type"] == "checkbox"
    assert fields["new_game"]["required"] is False
    assert fields["new_game"]["default"] is False
    assert tool_ui_schema["result_template"]["primary_field"] == "result"


def test_json_stdin_stdout_returns_contract_for_invalid_guess():
    completed = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps({"guess": "12345", "new_game": True}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    response = json.loads(completed.stdout)
    assert_output_contract(response)
    assert response["status"] == "error"
    assert response["attempts_used"] == 0
    assert response["attempts_remaining"] == 6
    assert response["feedback"] == []
    assert response["history"] == []
    assert "five alphabetic" in response["result"]


def test_in_progress_guesses_persist_history_and_new_game_resets(monkeypatch, tmp_path):
    skill = load_skill_module()
    monkeypatch.setattr(skill, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(skill, "_choose_answer", lambda: "CRANE")

    first = skill.run({"guess": "slate", "new_game": True})
    second = skill.run({"guess": "plant"})
    reset = skill.run({"guess": "brick", "new_game": True})

    for response in (first, second, reset):
        assert_output_contract(response)
        assert response["status"] == "in_progress"
        assert len(response["feedback"]) == 5

    assert first["attempts_used"] == 1
    assert first["attempts_remaining"] == 5
    assert [item["guess"] for item in second["history"]] == ["SLATE", "PLANT"]
    assert second["attempts_used"] == 2
    assert second["attempts_remaining"] == 4
    assert [item["guess"] for item in reset["history"]] == ["BRICK"]
    assert reset["attempts_used"] == 1
    assert reset["attempts_remaining"] == 5


def test_win_and_six_attempt_loss_return_terminal_counts(monkeypatch, tmp_path):
    skill = load_skill_module()
    monkeypatch.setattr(skill, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(skill, "_choose_answer", lambda: "CRANE")

    win = skill.run({"guess": "crane", "new_game": True})
    assert_output_contract(win)
    assert win["status"] == "won"
    assert win["attempts_used"] == 1
    assert win["attempts_remaining"] == 5
    assert [item["state"] for item in win["feedback"]] == ["correct"] * 5

    guesses = ["SLATE", "PLANT", "BRICK", "CLOUD", "SHINE", "GRAPE"]
    responses = [skill.run({"guess": guess, "new_game": index == 0}) for index, guess in enumerate(guesses)]
    loss = responses[-1]

    assert_output_contract(loss)
    assert loss["status"] == "lost"
    assert loss["attempts_used"] == 6
    assert loss["attempts_remaining"] == 0
    assert [item["guess"] for item in loss["history"]] == guesses
    assert "CRANE" in loss["result"]


def test_invalid_guess_does_not_add_history(monkeypatch, tmp_path):
    skill = load_skill_module()
    monkeypatch.setattr(skill, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(skill, "_choose_answer", lambda: "CRANE")

    accepted = skill.run({"guess": "slate", "new_game": True})
    invalid = skill.run({"guess": "toolong"})

    assert_output_contract(invalid)
    assert invalid["status"] == "error"
    assert invalid["attempts_used"] == 1
    assert invalid["attempts_remaining"] == 5
    assert invalid["feedback"] == []
    assert invalid["history"] == accepted["history"]


def test_duplicate_letter_feedback_does_not_overcount_present_letters(monkeypatch, tmp_path):
    skill = load_skill_module()
    monkeypatch.setattr(skill, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(skill, "_choose_answer", lambda: "BRICK")

    response = skill.run({"guess": "civic", "new_game": True})

    assert_output_contract(response)
    assert response["status"] == "in_progress"
    assert response["feedback"] == [
        {"letter": "C", "state": "present"},
        {"letter": "I", "state": "present"},
        {"letter": "V", "state": "absent"},
        {"letter": "I", "state": "absent"},
        {"letter": "C", "state": "absent"},
    ]
