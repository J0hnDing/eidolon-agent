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
    old = {"goal-1": {"title": "A", "progress": 20.5, "blueprint_nodes": []}}
    new = {
        "goal-1": {"title": "A", "progress": 40.25, "blueprint_nodes": [{"id": "step-1", "title": "First step", "progress": 0}]},
        "goal-2": {"title": "B", "progress": None, "blueprint_nodes": []},
    }
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {"goal_snapshot_schema": 2})
    assert metrics == {
        "goals_advanced": 1, "goal_coverage": 100.0, "new_goals": 1, "new_blueprint_steps": 1,
        "advanced_goal_names": ["A"], "new_goal_names": ["B"], "new_blueprint_step_names": ["A: First step"],
    }
    assert last == {"goal-1": "2026-09-21"}


def test_goal_tree_edit_without_progress_increase_is_not_advancement() -> None:
    old = {"goal-1": {"title": "A", "progress": 40, "blueprint_nodes": []}}
    new = {"goal-1": {"title": "A", "progress": 40, "blueprint_nodes": [{"id": "step-1", "title": "Plan", "progress": 0}]}}
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {"goal_snapshot_schema": 2})
    assert metrics["goals_advanced"] == 0
    assert metrics["goal_coverage"] == 0.0
    assert metrics["new_blueprint_steps"] == 1
    assert last == {}


def test_goal_reaching_full_progress_counts_in_prior_active_cohort() -> None:
    old = {"goal-1": {"title": "A", "progress": 80, "blueprint_nodes": []}}
    new = {"goal-1": {"title": "A", "progress": 100, "blueprint_nodes": []}}
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {"goal_snapshot_schema": 2})
    assert metrics["goals_advanced"] == 1
    assert metrics["goal_coverage"] == 100.0
    assert last == {"goal-1": "2026-09-21"}


def test_goal_snapshot_accepts_current_blueprints_and_unassessed_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        skill, "_call",
        lambda *_args: {"goals": [
            {"id": "root", "title": "Root", "progress": 30.5, "blueprint": {"nodes": [{"id": "step", "title": "Step", "progress": 30.5}], "edges": []}},
            {"id": "unassessed", "title": "Unassessed", "progress": None, "blueprint": {"nodes": [], "edges": []}},
        ], "graph": {"edges": []}},
    )
    assert skill._goal_snapshot() == {
        "root": {"title": "Root", "progress": 30.5, "blueprint_nodes": [{"id": "step", "title": "Step", "progress": 30.5}]},
        "unassessed": {"title": "Unassessed", "progress": None, "blueprint_nodes": []},
    }


@pytest.mark.parametrize("progress", [True, -1, 101, "50", float("nan"), float("inf")])
def test_goal_snapshot_rejects_invalid_progress(monkeypatch: pytest.MonkeyPatch, progress: object) -> None:
    monkeypatch.setattr(skill, "_call", lambda *_args: {"goals": [
        {"id": "root", "title": "Root", "progress": progress, "blueprint": {"nodes": [], "edges": []}},
    ]})
    with pytest.raises(ValueError, match="invalid Goal progress"):
        skill._goal_snapshot()


def test_unassessed_progress_is_not_invented_or_counted_as_advancement() -> None:
    old = {
        "unknown": {"title": "Unknown", "progress": None, "blueprint_nodes": []},
        "assessed": {"title": "Assessed", "progress": 20, "blueprint_nodes": []},
    }
    new = {
        "unknown": {"title": "Unknown", "progress": 50, "blueprint_nodes": []},
        "assessed": {"title": "Assessed", "progress": None, "blueprint_nodes": []},
    }
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {"goal_snapshot_schema": 2})
    assert metrics["goals_advanced"] == 0
    assert metrics["goal_coverage"] is None
    assert last == {}
    metrics = {**dict.fromkeys(skill.METRIC_KEYS), **metrics}
    blocks = skill._report_blocks(metrics, new, last, [{"week_start": "2026-09-21", "metrics": metrics}], True)
    text = json.dumps(blocks)
    assert "Assessed (unassessed)" in text
    assert "None%" not in text


def test_blueprint_step_ids_are_scoped_to_the_owning_goal() -> None:
    old = {"a": {"title": "A", "progress": None, "blueprint_nodes": [{"id": "step", "title": "First", "progress": 0}]}}
    new = {**old, "b": {"title": "B", "progress": None, "blueprint_nodes": [{"id": "step", "title": "Second", "progress": 0}]}}
    metrics, _ = skill._goal_metrics(new, skill.date(2026, 9, 21), {"goals": old}, {"goal_snapshot_schema": 2})
    assert metrics["new_blueprint_steps"] == 1
    assert metrics["new_blueprint_step_names"] == ["B: Second"]


def test_legacy_goal_cache_establishes_baseline_and_preserves_other_history() -> None:
    old = {"a": {"title": "A", "progress": 20, "children": [{"id": "sub", "title": "Old subgoal"}]}}
    new = {"a": {"title": "A", "progress": 30.5, "blueprint_nodes": [{"id": "step", "title": "Step", "progress": 30.5}]}}
    state = {
        "goals": old, "last_advanced": {"a": "2026-09-07"},
        "history": [{"week_start": "2026-09-14", "metrics": {"goals_advanced": 1, "goal_coverage": 100, "scheduled_hours": 3, "new_subgoals": 2}}],
    }
    metrics, last = skill._goal_metrics(new, skill.date(2026, 9, 21), state, state)
    assert metrics["goals_advanced"] is None
    assert metrics["goal_coverage"] is None
    assert metrics["new_blueprint_steps"] is None
    assert metrics["new_goals"] == 0
    assert last == {}
    history = skill._history(state, skill.date(2026, 9, 21), metrics)
    assert history[0]["metrics"]["scheduled_hours"] == 3
    assert history[0]["metrics"]["goals_advanced"] is None
    assert "new_blueprint_steps" not in history[0]["metrics"]


def test_run_creates_report_then_persists_rolling_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    calls: list[str] = []

    def fake_call(*, operation: str, input: dict) -> dict:  # noqa: A002
        calls.append(operation)
        if operation == "notion.todo.list":
            return {"todos": [], "has_more": False, "next_cursor": None}
        if operation == "atlas.goal.list":
            return {"goals": [], "graph": {"edges": []}}
        if operation == "google_calendar.event.list":
            return {"events": [], "has_more": False, "next_page_token": None}
        assert operation == "notion.report.create"
        assert input["select"] == "Personal Feed"
        assert input["name"].startswith("Personal Weekly Summary")
        assert len(input["children"]) <= 100
        assert all(block["type"] != "heading_1" for block in input["children"])
        assert [
            block["heading_2"]["rich_text"][0]["text"]["content"]
            for block in input["children"] if block["type"] == "heading_2"
        ] == ["Todos", "Goals", "Calendar"]
        return {"id": "report-1", "name": input["name"], "select": input["select"], "created_time": "2026-09-27T21:00:00Z"}

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", fake_call)
    now = datetime(2026, 9, 27, 21, tzinfo=ZoneInfo("America/Toronto"))
    result = skill.run({}, now=now)
    assert result == {"week_start": "2026-09-21", "report_id": "report-1", "already_reported": False}
    assert calls == ["notion.todo.list", "atlas.goal.list", "google_calendar.event.list", "notion.report.create"]
    state = json.loads((tmp_path / skill.STATE_FILE).read_text(encoding="utf-8"))
    assert state["history"][0]["week_start"] == "2026-09-21"
    assert state["history"][0]["metrics"]["completion_rate"] is None
    assert state["goal_snapshot_schema"] == 2
    assert skill.run({}, now=now)["already_reported"] is True
    assert len(calls) == 4
