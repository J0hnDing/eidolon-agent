import importlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
SKILL_PATH = ROOT / "skill.py"


@pytest.fixture()
def skill_module(monkeypatch, tmp_path):
    skill = importlib.import_module("skill")
    monkeypatch.setattr(skill, "CACHE_DIR", tmp_path / "cache")
    return skill


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def assert_wordle_output_shape(payload):
    assert set(payload) == {
        "result",
        "session_id",
        "guess",
        "feedback",
        "attempts_used",
        "attempts_remaining",
        "game_status",
        "answer",
    }
    assert isinstance(payload["result"], str)
    assert isinstance(payload["session_id"], str)
    assert isinstance(payload["guess"], str)
    assert isinstance(payload["feedback"], list)
    assert isinstance(payload["attempts_used"], int)
    assert isinstance(payload["attempts_remaining"], int)
    assert payload["game_status"] in {"in_progress", "won", "lost"}
    assert payload["answer"] is None or (
        isinstance(payload["answer"], str)
        and len(payload["answer"]) == 5
        and payload["answer"].isalpha()
    )
    for item in payload["feedback"]:
        assert set(item) == {"letter", "status"}
        assert len(item["letter"]) == 1
        assert item["letter"].isalpha()
        assert item["status"] in {"correct", "present", "absent"}


def test_manifest_contract_permissions_and_tool_ui_schema():
    manifest = load_manifest()

    assert manifest["name"] == "wordle_game"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["dependencies"] == []
    assert manifest["risk_level"] == "low"
    assert manifest["permissions"] == {
        "network": [],
        "filesystem_read": [],
        "filesystem_write": ["./cache"],
        "secrets": [],
        "shell": False,
    }

    tool_ui_schema = manifest["tool_ui_schema"]
    assert isinstance(tool_ui_schema, dict)
    assert isinstance(tool_ui_schema.get("fields"), list)
    fields_by_name = {field["name"]: field for field in tool_ui_schema["fields"]}
    assert {"guess", "session_id", "new_game"} <= set(fields_by_name)
    assert fields_by_name["guess"]["required"] is True
    assert fields_by_name["new_game"]["type"] in {"checkbox", "boolean"}

    input_schema = manifest["input_schema"]
    assert input_schema["required"] == ["guess"]
    assert input_schema["additionalProperties"] is False
    assert input_schema["properties"]["guess"]["pattern"] == "^[A-Za-z]{5}$"

    output_schema = manifest["output_schema"]
    assert "feedback" in output_schema["required"]
    assert output_schema["properties"]["game_status"]["enum"] == [
        "in_progress",
        "won",
        "lost",
    ]


def test_json_stdin_stdout_contract_and_invalid_guess_rejection():
    session_id = f"pytest-json-{uuid.uuid4().hex}"
    completed = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps({"guess": "crane", "session_id": session_id, "new_game": True}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    output = json.loads(completed.stdout)
    assert_wordle_output_shape(output)
    assert output["session_id"] == session_id
    assert output["guess"] == "CRANE"
    assert len(output["feedback"]) == 5
    assert output["attempts_used"] == 1
    assert output["attempts_remaining"] == 5
    assert output["answer"] is None or output["game_status"] in {"won", "lost"}

    invalid = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps({"guess": "cranes"}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert invalid.returncode == 0
    invalid_output = json.loads(invalid.stdout)
    assert_wordle_output_shape(invalid_output)
    assert "exactly five alphabetic" in invalid_output["result"]
    assert invalid_output["guess"] == ""
    assert invalid_output["feedback"] == []


def test_default_named_session_continuity_and_new_game_reset(skill_module):
    first_default = skill_module.play({"guess": "zzzzz", "new_game": True})
    second_default = skill_module.play({"guess": "yyyyy", "session_id": "   "})

    assert first_default["session_id"] == "default"
    assert second_default["session_id"] == "default"
    assert first_default["attempts_used"] == 1
    assert second_default["attempts_used"] == 2
    assert first_default["answer"] is None
    assert second_default["answer"] is None

    session_id = "named-continuity"
    first_named = skill_module.play(
        {"guess": "zzzzz", "session_id": session_id, "new_game": True}
    )
    second_named = skill_module.play({"guess": "yyyyy", "session_id": session_id})
    reset_named = skill_module.play(
        {"guess": "xxxxx", "session_id": session_id, "new_game": True}
    )

    assert first_named["attempts_used"] == 1
    assert second_named["attempts_used"] == 2
    assert reset_named["attempts_used"] == 1
    assert reset_named["attempts_remaining"] == 5
    assert reset_named["game_status"] == "in_progress"
    assert reset_named["answer"] is None


def test_duplicate_letter_feedback_uses_wordle_count_limits(skill_module):
    feedback = skill_module.score_guess("ALLEY", "APPLE")

    assert feedback == [
        {"letter": "A", "status": "correct"},
        {"letter": "L", "status": "present"},
        {"letter": "L", "status": "absent"},
        {"letter": "E", "status": "present"},
        {"letter": "Y", "status": "absent"},
    ]


def test_win_loss_and_finished_sessions_reveal_answer_and_stop_changing(skill_module):
    win_session = "win-session"
    win_answer = skill_module.choose_answer(win_session, 2)
    won = skill_module.play(
        {"guess": win_answer, "session_id": win_session, "new_game": True}
    )
    after_win = skill_module.play({"guess": "zzzzz", "session_id": win_session})

    assert won["game_status"] == "won"
    assert won["answer"] == win_answer
    assert won["attempts_used"] == 1
    assert all(item["status"] == "correct" for item in won["feedback"])
    assert after_win["game_status"] == "won"
    assert after_win["attempts_used"] == 1
    assert after_win["feedback"] == []
    assert after_win["answer"] == win_answer

    loss_session = "loss-session"
    loss_answer = skill_module.choose_answer(loss_session, 2)
    outputs = [
        skill_module.play(
            {"guess": "zzzzz", "session_id": loss_session, "new_game": attempt == 0}
        )
        for attempt in range(6)
    ]
    after_loss = skill_module.play({"guess": "yyyyy", "session_id": loss_session})

    assert outputs[-1]["game_status"] == "lost"
    assert outputs[-1]["attempts_used"] == 6
    assert outputs[-1]["attempts_remaining"] == 0
    assert outputs[-1]["answer"] == loss_answer
    assert after_loss["game_status"] == "lost"
    assert after_loss["attempts_used"] == 6
    assert after_loss["feedback"] == []
    assert after_loss["answer"] == loss_answer
