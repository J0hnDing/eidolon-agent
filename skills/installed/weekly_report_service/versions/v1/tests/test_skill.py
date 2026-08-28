from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
for parent in Path(__file__).resolve().parents:
    backend_dir = parent / "backend"
    if (backend_dir / "function_runtime_capabilities.py").is_file():
        sys.path.insert(0, str(backend_dir))
        break
sys.path.insert(0, str(SKILL_DIR))

import skill  # noqa: E402


def _repository(**overrides):
    repository = {
        "name": "example/project",
        "url": "https://github.com/example/project",
        "stars": 1234,
        "description": "A useful project.",
        "analysis": "A concise grounded analysis with a useful local lesson.",
    }
    repository.update(overrides)
    return repository


def _created_report(name: str):
    return {
        "id": "report-id",
        "name": name,
        "created_time": "2026-08-31T12:00:00Z",
        "select": "GitHub Projects",
    }


def test_run_uses_fixed_scout_input_and_creates_native_notion_blocks(monkeypatch):
    function_calls = []
    integration_calls = []
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_function",
        lambda name, input_json: function_calls.append((name, input_json)) or {"repositories": [_repository()]},
    )

    def create_report(**kwargs):
        integration_calls.append(kwargs)
        return _created_report(kwargs["input"]["name"])

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", create_report)

    result = skill.run({}, now=datetime(2026, 8, 31, 8, 0, tzinfo=ZoneInfo("America/Toronto")))

    assert function_calls == [(skill.SCOUT_FUNCTION, skill.SCOUT_INPUT)]
    assert integration_calls[0]["operation"] == "notion.report.create"
    create_input = integration_calls[0]["input"]
    assert create_input["name"] == "Weekly GitHub Projects Report — 2026-08-31"
    assert create_input["select"] == "GitHub Projects"
    assert [block["type"] for block in create_input["children"]] == [
        "heading_1", "callout", "divider", "heading_2", "paragraph", "paragraph", "bookmark", "divider"
    ]
    assert create_input["children"][3]["heading_2"]["rich_text"][0]["text"]["link"] == {
        "url": "https://github.com/example/project"
    }
    assert result == {"report": _created_report(create_input["name"]), "repository_count": 1}


def test_empty_scout_result_creates_explicit_empty_report(monkeypatch):
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_function",
        lambda *_args, **_kwargs: {"repositories": []},
    )
    captured = {}

    def create_report(**kwargs):
        captured.update(kwargs["input"])
        return _created_report(kwargs["input"]["name"])

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", create_report)

    result = skill.run({}, now=datetime(2026, 8, 31, tzinfo=ZoneInfo("America/Toronto")))

    assert result["repository_count"] == 0
    assert captured["children"][-1]["paragraph"]["rich_text"][0]["text"]["content"].startswith(
        "GitHub Scout returned no repositories"
    )


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"repositories": {}},
        {"repositories": [{"name": "missing-fields"}]},
        {"repositories": [_repository(stars=True)]},
        {"repositories": [_repository(analysis="")]},
        {"repositories": [_repository(url="https://example.com/not-github")]},
    ],
)
def test_invalid_scout_output_fails_before_notion_create(monkeypatch, output):
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_function", lambda *_args, **_kwargs: output)

    def unexpected_create(**_kwargs):
        raise AssertionError("Notion create must not run for invalid Scout output")

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", unexpected_create)

    with pytest.raises(ValueError):
        skill.run({})


def test_service_rejects_input_and_has_no_direct_codex_call_surface(monkeypatch):
    assert not hasattr(skill, "call_codex")
    with pytest.raises(ValueError, match="empty object"):
        skill.run({"unexpected": True})


def test_scout_failure_propagates_without_notion_or_codex_fallback(monkeypatch):
    def failed_scout(*_args, **_kwargs):
        raise RuntimeError("scout failed")

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_function", failed_scout)
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        lambda **_kwargs: pytest.fail("Notion must not run after Scout fails"),
    )

    with pytest.raises(RuntimeError, match="scout failed"):
        skill.run({})


def test_notion_failure_propagates_without_direct_codex_fallback(monkeypatch):
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_function",
        lambda *_args, **_kwargs: {"repositories": [_repository()]},
    )

    def failed_notion(**_kwargs):
        raise RuntimeError("notion failed")

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", failed_notion)

    with pytest.raises(RuntimeError, match="notion failed"):
        skill.run({})
