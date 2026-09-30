from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
for parent in Path(__file__).resolve().parents:
    backend = parent / "backend"
    if (backend / "integration_runtime_capabilities.py").is_file():
        sys.path.insert(0, str(backend))
        break
sys.path.insert(0, str(SKILL_DIR))

import skill  # noqa: E402


def _todo(todo_id: str, *, done: bool, created: str, edited: str, due: str | None = None) -> dict:
    return {
        "id": todo_id, "done": done, "archived": done,
        "created_at": created, "last_edited_at": edited, "due_at": due,
    }


def _event(start: str, end: str) -> dict:
    return {
        "status": "confirmed",
        "start": {"date_time": start}, "end": {"date_time": end},
    }


def test_calendar_merges_overlaps_and_finds_free_block() -> None:
    events = [
        _event("2026-09-21T10:00:00-04:00", "2026-09-21T12:00:00-04:00"),
        _event("2026-09-21T11:00:00-04:00", "2026-09-21T13:00:00-04:00"),
        {"status": "confirmed", "start": {"date": "2026-09-22"}, "end": {"date": "2026-09-23"}},
    ]
    result = skill._calendar_metrics(events, skill.date(2026, 9, 21), skill.date(2026, 9, 28), 540, 1440)
    assert result == {"scheduled_hours": 3.0, "occupation_rate": 4.0, "longest_free_hours": 15.0}


def test_todo_cohort_uses_open_snapshot_and_new_tasks() -> None:
    previous = {"open_ids": ["old", "completed", "removed"]}
    todos = [
        _todo("old", done=False, created="2026-09-01T10:00:00Z", edited="2026-09-21T10:00:00Z"),
        _todo("completed", done=True, created="2026-09-01T10:00:00Z", edited="2026-09-22T10:00:00Z", due="2026-09-22"),
        _todo("new", done=True, created="2026-09-23T10:00:00Z", edited="2026-09-25T10:00:00Z", due="2026-09-24"),
    ]
    metrics, open_ids = skill._todo_metrics(
        todos, skill.date(2026, 9, 21), datetime(2026, 9, 27, 21, tzinfo=ZoneInfo("America/Toronto")), previous,
    )
    assert metrics == {"completion_rate": 66.7, "on_time_rate": 50.0, "carryover_rate": 33.3}
    assert open_ids == {"old"}


def test_goal_snapshot_compares_progress_and_keeps_new_goals_separate() -> None:
    old = {"goal-1": {"title": "A", "progress": 20, "children": []}}
    new = {
        "goal-1": {"title": "A", "progress": 40, "children": [{"id": "sub-1", "parent": "goal-1", "title": "First step", "progress": 0}]},
        "goal-2": {"title": "B", "progress": 0, "children": []},
    }
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {})
    assert metrics == {
        "goals_advanced": 1, "goal_coverage": 100.0, "new_goals": 1, "new_subgoals": 1,
        "advanced_goal_names": ["A"], "new_goal_names": ["B"], "new_subgoal_names": ["First step"],
    }
    assert last == {"goal-1": "2026-09-21"}


def test_goal_tree_edit_without_progress_increase_is_not_advancement() -> None:
    old = {"goal-1": {"title": "A", "progress": 40, "children": []}}
    new = {"goal-1": {"title": "A", "progress": 40, "children": [{"id": "sub-1", "parent": "goal-1", "title": "Plan", "progress": 0}]}}
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {})
    assert metrics["goals_advanced"] == 0
    assert metrics["goal_coverage"] == 0.0
    assert metrics["new_subgoals"] == 1
    assert last == {}


def test_goal_reaching_full_progress_counts_in_prior_active_cohort() -> None:
    old = {"goal-1": {"title": "A", "progress": 80, "children": []}}
    new = {"goal-1": {"title": "A", "progress": 100, "children": []}}
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {})
    assert metrics["goals_advanced"] == 1
    assert metrics["goal_coverage"] == 100.0
    assert last == {"goal-1": "2026-09-21"}


def test_goal_progress_snapshot_validates_recursive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        skill, "_call",
        lambda *_args: {"goals": [{"id": "root", "title": "Root", "progress": 50, "subgoals": [{"id": "child", "title": "Child", "progress": 30, "subgoals": []}]}]},
    )
    assert skill._goal_snapshot()["root"]["progress"] == 50
    assert skill._goal_snapshot()["root"]["children"][0]["progress"] == 30


def test_run_creates_report_then_persists_rolling_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    calls: list[str] = []

    def fake_call(*, operation: str, input: dict) -> dict:  # noqa: A002
        calls.append(operation)
        if operation == "notion.todo.list":
            return {"todos": [], "has_more": False, "next_cursor": None}
        if operation == "atlas.goal.list":
            return {"goals": [], "progressions": []}
        if operation == "google_calendar.event.list":
            return {"events": [], "has_more": False, "next_page_token": None}
        assert operation == "notion.report.create"
        assert input["select"] == "Personal Feed"
        assert input["name"].startswith("Personal Weekly Summary")
        assert len(input["children"]) <= 100
        return {"id": "report-1", "name": input["name"], "select": input["select"], "created_time": "2026-09-27T21:00:00Z"}

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", fake_call)
    now = datetime(2026, 9, 27, 21, tzinfo=ZoneInfo("America/Toronto"))
    result = skill.run({}, now=now)
    assert result == {"week_start": "2026-09-21", "report_id": "report-1", "already_reported": False}
    assert calls == ["notion.todo.list", "atlas.goal.list", "google_calendar.event.list", "notion.report.create"]
    state = json.loads((tmp_path / skill.STATE_FILE).read_text(encoding="utf-8"))
    assert state["history"][0]["week_start"] == "2026-09-21"
    assert state["history"][0]["metrics"]["completion_rate"] is None
    assert skill.run({}, now=now)["already_reported"] is True
    assert len(calls) == 4
