import json
import subprocess
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_PATH = SKILL_DIR / "skill.py"


def run_skill(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        cwd=SKILL_DIR,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_filters_ranks_and_respects_max_items() -> None:
    digest = run_skill(
        {
            "topics": ["AI infrastructure", "Nvidia", "data centers"],
            "max_items": 2,
        }
    )

    assert digest["title"] == "Personal News Digest"
    assert digest["topics"] == ["AI infrastructure", "Nvidia", "data centers"]
    assert len(digest["items"]) == 2
    assert digest["items"][0]["headline"] == "Nvidia expands AI infrastructure platform for dense data centers"
    assert digest["items"][0]["matched_topics"] == [
        "AI infrastructure",
        "Nvidia",
        "data centers",
    ]


def test_deduplicates_by_url() -> None:
    digest = run_skill(
        {
            "topics": ["Nvidia", "AI infrastructure"],
            "max_items": 10,
        }
    )
    urls = [item["url"] for item in digest["items"]]

    assert len(urls) == len(set(urls))
    assert urls.count("https://local.example/articles/nvidia-ai-infrastructure-data-centers") == 1


def test_unknown_topics_return_warning_and_no_items() -> None:
    digest = run_skill({"topics": ["ornamental gardening"], "max_items": 5})

    assert digest["items"] == []
    assert "No local sample articles matched the requested topics" in digest["warnings"]


def test_empty_input_uses_default_topics() -> None:
    result = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        cwd=SKILL_DIR,
        input="{}",
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    digest = json.loads(result.stdout)

    assert result.returncode == 0
    assert digest["topics"]
    assert digest["items"]


def test_invalid_json_returns_json_warning() -> None:
    result = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        cwd=SKILL_DIR,
        input="{bad json",
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    digest = json.loads(result.stdout)

    assert result.returncode == 0
    assert digest["items"] == []
    assert digest["warnings"]
