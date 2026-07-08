import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
SKILL_PATH = ROOT / "skill.py"


REQUIRED_OUTPUT_KEYS = {
    "status",
    "result",
    "attempts_used",
    "attempts_remaining",
    "feedback",
    "history",
}


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def run_skill_subprocess(payload, cache_dir):
    runner = (
        "import importlib.util, json, pathlib, sys\n"
        "skill_path = pathlib.Path(sys.argv[1])\n"
        "cache_dir = pathlib.Path(sys.argv[2])\n"
        "spec = importlib.util.spec_from_file_location('wordle_skill_under_test', skill_path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module.CACHE_DIR = cache_dir\n"
        "raise SystemExit(module.main())\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", runner, str(SKILL_PATH), str(cache_dir)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def assert_output_contract(response):
    assert set(response) == REQUIRED_OUTPUT_KEYS
    assert response["status"] in {"in_progress", "won", "lost", "reset", "error"}
    assert isinstance(response["result"], str)
    assert isinstance(response["attempts_used"], int)
    assert 0 <= response["attempts_used"] <= 6
    assert isinstance(response["attempts_remaining"], int)
    assert 0 <= response["attempts_remaining"] <= 6
    assert response["attempts_used"] + response["attempts_remaining"] == 6
    assert isinstance(response["feedback"], list)
    assert isinstance(response["history"], list)
    for item in response["feedback"]:
        assert set(item) == {"letter", "state"}
        assert len(item["letter"]) == 1
        assert item["letter"].isalpha()
        assert item["letter"].isupper()
        assert item["state"] in {"correct", "present", "absent"}


def test_manifest_tool_contract_and_permissions_match_blueprint():
    manifest = load_manifest()

    assert manifest["name"] == "wordle_game"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
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
    assert output_schema["additionalProperties"] is False
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


def test_manifest_declares_renderable_tool_ui_schema():
    tool_ui_schema = load_manifest()["tool_ui_schema"]

    assert tool_ui_schema["title"] == "Wordle Game"
    assert tool_ui_schema["submit_label"] == "Submit Guess"
    assert tool_ui_schema["result_template"] == {
        "primary_field": "result",
        "primary_label": "Result",
    }

    fields = {field["name"]: field for field in tool_ui_schema["fields"]}
    assert set(fields) == {"guess", "new_game"}
    assert fields["guess"]["label"] == "Guess"
    assert fields["guess"]["type"] == "text"
    assert fields["guess"]["placeholder"] == "CRANE"
    assert fields["guess"]["required"] is True
    assert fields["new_game"]["label"] == "Start New Game"
    assert fields["new_game"]["type"] == "checkbox"
    assert fields["new_game"]["required"] is False
    assert fields["new_game"]["default"] is False


def test_json_stdin_stdout_gameplay_persists_history_and_resets_with_new_game(tmp_path):
    cache_dir = tmp_path / "cache"

    first = run_skill_subprocess({"guess": "crane", "new_game": True}, cache_dir)
    assert_output_contract(first)
    assert first["status"] in {"in_progress", "won"}
    assert first["attempts_used"] == 1
    assert first["attempts_remaining"] == 5
    assert [item["letter"] for item in first["feedback"]] == list("CRANE")
    assert [entry["guess"] for entry in first["history"]] == ["CRANE"]

    second = run_skill_subprocess({"guess": "slate"}, cache_dir)
    assert_output_contract(second)
    if first["status"] == "won":
        assert second["attempts_used"] == 1
        assert [entry["guess"] for entry in second["history"]] == ["SLATE"]
    else:
        assert second["attempts_used"] == 2
        assert [entry["guess"] for entry in second["history"]] == ["CRANE", "SLATE"]

    reset = run_skill_subprocess({"guess": "plant", "new_game": True}, cache_dir)
    assert_output_contract(reset)
    assert reset["attempts_used"] == 1
    assert [entry["guess"] for entry in reset["history"]] == ["PLANT"]


def test_invalid_guess_is_rejected_without_consuming_attempt(tmp_path):
    cache_dir = tmp_path / "cache"

    opening = run_skill_subprocess({"guess": "crane", "new_game": True}, cache_dir)
    invalid = run_skill_subprocess({"guess": "TOO-LONG"}, cache_dir)

    assert_output_contract(invalid)
    assert invalid["status"] == "error"
    assert invalid["result"] == "Guess must be exactly five alphabetic characters."
    assert invalid["feedback"] == []
    assert invalid["attempts_used"] == opening["attempts_used"]
    assert invalid["attempts_remaining"] == opening["attempts_remaining"]
    assert invalid["history"] == opening["history"]


def test_duplicate_letter_feedback_does_not_overcount(monkeypatch, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("wordle_skill_duplicate_test", SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(module, "_choose_answer", lambda: "CRANE")

    response = module.run({"guess": "cocoa", "new_game": True})

    assert_output_contract(response)
    assert response["status"] == "in_progress"
    assert response["feedback"] == [
        {"letter": "C", "state": "correct"},
        {"letter": "O", "state": "absent"},
        {"letter": "C", "state": "absent"},
        {"letter": "O", "state": "absent"},
        {"letter": "A", "state": "present"},
    ]


@pytest.mark.parametrize(
    ("answer", "guesses", "expected_status", "expected_used", "expected_remaining"),
    [
        ("BRICK", ["crane", "brick"], "won", 2, 4),
        ("BRICK", ["crane", "slate", "plant", "cloud", "grape", "mirth"], "lost", 6, 0),
    ],
)
def test_terminal_win_and_loss_states(monkeypatch, tmp_path, answer, guesses, expected_status, expected_used, expected_remaining):
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"wordle_skill_{expected_status}_test", SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(module, "_choose_answer", lambda: answer)

    response = None
    for index, guess in enumerate(guesses):
        response = module.run({"guess": guess, "new_game": index == 0})

    assert response is not None
    assert_output_contract(response)
    assert response["status"] == expected_status
    assert response["attempts_used"] == expected_used
    assert response["attempts_remaining"] == expected_remaining
    assert len(response["history"]) == expected_used
    if expected_status == "won":
        assert response["feedback"] == [
            {"letter": letter, "state": "correct"} for letter in answer
        ]
    else:
        assert "The answer was BRICK" in response["result"]
