"""Deterministic Sunday Personal Feed report. No model or generated prose is used."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import integration_runtime_capabilities

ZONE = ZoneInfo("America/Toronto")
STATE_FILE = "personal_weekly_summary.json"
MAX_PAGES = 100
PAGE_SIZE = 100
RATE_KEYS = ("completion_rate", "on_time_rate", "carryover_rate", "goal_coverage", "occupation_rate")
METRIC_KEYS = (
    "completion_rate", "on_time_rate", "carryover_rate", "goals_advanced",
    "goal_coverage", "new_goals", "new_subgoals", "scheduled_hours",
    "occupation_rate", "longest_free_hours",
)
SPARK = "▁▂▃▄▅▆▇█"


def run(input_json: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(input_json, dict) or set(input_json) - {"active_start", "active_end"}:
        raise ValueError("Personal weekly summary input is invalid")
    active_start = _clock(input_json.get("active_start", "09:00"))
    active_end = _clock(input_json.get("active_end", "24:00"), allow_24=True)
    if active_end <= active_start:
        raise ValueError("Active hours must end after they start")
    current = now or datetime.now(ZONE)
    if current.tzinfo is None:
        raise ValueError("Current time must be timezone aware")
    current = current.astimezone(ZONE)
    week_start = current.date() - timedelta(days=current.weekday())
    week_end = week_start + timedelta(days=7)
    state = _load_state()
    if state.get("week_start") == week_start.isoformat():
        report_id = state.get("report_id")
        if not isinstance(report_id, str) or not report_id:
            raise ValueError("Personal weekly summary state is invalid")
        return {"week_start": week_start.isoformat(), "report_id": report_id, "already_reported": True}

    todos = _all_todos()
    goals = _goal_snapshot()
    events = _all_events(week_start, week_end)
    previous_week = week_start - timedelta(days=7)
    comparable = state.get("week_start") == previous_week.isoformat()
    previous = state if comparable else None
    metrics, open_ids = _todo_metrics(todos, week_start, current, previous)
    goal_metrics, last_advanced = _goal_metrics(goals, week_start, previous, state)
    metrics.update(goal_metrics)
    metrics.update(_calendar_metrics(events, week_start, week_end, active_start, active_end))
    history = _history(state, week_start, metrics)
    name = f"Personal Weekly Summary — {week_start.isoformat()} to {(week_end - timedelta(days=1)).isoformat()}"
    children = _report_blocks(name, metrics, goals, last_advanced, history, comparable)
    if len(children) > 100 or len(json.dumps(children).encode("utf-8")) > 500_000:
        raise ValueError("Personal weekly summary report exceeds Notion limits")
    report = _call("notion.report.create", {"name": name, "select": "Personal Feed", "children": children})
    if (
        not isinstance(report, dict)
        or report.get("name") != name
        or report.get("select") != "Personal Feed"
        or not isinstance(report.get("id"), str)
        or not report["id"]
    ):
        raise ValueError("Notion returned invalid Personal Feed report metadata")
    _save_state({
        "version": 1,
        "week_start": week_start.isoformat(),
        "report_id": report["id"],
        "open_ids": sorted(open_ids),
        "goals": goals,
        "last_advanced": last_advanced,
        "history": history,
    })
    return {"week_start": week_start.isoformat(), "report_id": report["id"], "already_reported": False}


def _call(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = integration_runtime_capabilities.call(operation=operation, input=payload)
    if not isinstance(result, dict):
        raise ValueError(f"{operation} returned an invalid result")
    return result


def _all_todos() -> list[dict[str, Any]]:
    return _pages("notion.todo.list", "todos", "start_cursor", "next_cursor")


def _all_events(start: date, end: date) -> list[dict[str, Any]]:
    lower = datetime.combine(start, time.min, ZONE).isoformat()
    upper = datetime.combine(end, time.min, ZONE).isoformat()
    return _pages(
        "google_calendar.event.list", "events", "page_token", "next_page_token",
        {"time_min": lower, "time_max": upper},
    )


def _pages(
    operation: str, item_key: str, token_input: str, token_output: str,
    fixed: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        payload = {**(fixed or {}), "page_size": PAGE_SIZE}
        if token is not None:
            payload[token_input] = token
        page = _call(operation, payload)
        rows = page.get(item_key)
        has_more = page.get("has_more")
        next_token = page.get(token_output)
        if (
            not isinstance(rows, list) or len(rows) > PAGE_SIZE
            or any(not isinstance(row, dict) for row in rows)
            or not isinstance(has_more, bool)
        ):
            raise ValueError(f"{operation} returned invalid pagination")
        items.extend(rows)
        if not has_more:
            return items
        if not isinstance(next_token, str) or not next_token or next_token in seen:
            raise ValueError(f"{operation} returned invalid pagination")
        seen.add(next_token)
        token = next_token
    raise ValueError(f"{operation} exceeded the pagination limit")


def _goal_snapshot() -> dict[str, dict[str, Any]]:
    response = _call("atlas.goal.list", {"limit": 100})
    roots = response.get("goals")
    if not isinstance(roots, list) or len(roots) > 100:
        raise ValueError("Atlas returned invalid goals")
    result: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for root in roots:
        if not isinstance(root, dict):
            raise ValueError("Atlas returned invalid goals")
        root_id = root.get("id")
        if not isinstance(root_id, str) or not root_id or root_id in seen_ids:
            raise ValueError("Atlas returned duplicate or invalid goal IDs")
        children: list[dict[str, Any]] = []
        pending = [(root, None)]
        while pending:
            goal, parent = pending.pop()
            goal_id = goal.get("id")
            if not isinstance(goal_id, str) or not goal_id or goal_id in seen_ids:
                raise ValueError("Atlas returned duplicate or invalid goal IDs")
            progress = goal.get("progress")
            if not isinstance(progress, int) or isinstance(progress, bool) or not 0 <= progress <= 100:
                raise ValueError("Atlas returned invalid Goal progress")
            seen_ids.add(goal_id)
            subgoals = goal.get("subgoals")
            if not isinstance(subgoals, list) or any(not isinstance(child, dict) for child in subgoals):
                raise ValueError("Atlas returned invalid subgoals")
            if parent is not None:
                children.append({
                    "id": goal_id, "parent": parent,
                    "title": _short(goal.get("title")),
                    "progress": progress,
                    "description": goal.get("description"),
                    "target_date": goal.get("target_date"),
                })
            pending.extend((child, goal_id) for child in subgoals)
        result[root_id] = {
            "title": _short(root.get("title")),
            "progress": root["progress"],
            "children": sorted(children, key=lambda child: child["id"]),
        }
    return result


def _todo_metrics(
    todos: list[dict[str, Any]], week_start: date, cutoff: datetime,
    previous: dict[str, Any] | None,
) -> tuple[dict[str, float | int | None], set[str]]:
    current: dict[str, dict[str, Any]] = {}
    for todo in todos:
        todo_id = todo.get("id")
        if not isinstance(todo_id, str) or not todo_id or todo_id in current:
            raise ValueError("Notion returned duplicate or invalid todo IDs")
        if not isinstance(todo.get("done"), bool) or not isinstance(todo.get("archived"), bool):
            raise ValueError("Notion returned invalid todo state")
        _timestamp(todo.get("created_at"))
        _timestamp(todo.get("last_edited_at"))
        current[todo_id] = todo
    open_ids = {todo_id for todo_id, todo in current.items() if not todo["done"] and not todo["archived"]}
    if previous is None:
        return {"completion_rate": None, "on_time_rate": None, "carryover_rate": None}, open_ids
    old_open = previous.get("open_ids")
    if not isinstance(old_open, list) or any(not isinstance(item, str) for item in old_open):
        raise ValueError("Personal weekly summary state has invalid todo IDs")
    start = datetime.combine(week_start, time.min, ZONE)
    new_ids = {
        todo_id for todo_id, todo in current.items()
        if start <= _timestamp(todo["created_at"]).astimezone(ZONE) <= cutoff
    }
    cohort = {
        todo_id for todo_id in (set(old_open) | new_ids) & current.keys()
        if current[todo_id]["done"] or not current[todo_id]["archived"]
    }
    completed = [current[todo_id] for todo_id in cohort if current[todo_id]["done"]]
    due_completed = [todo for todo in completed if todo.get("due_at") is not None]
    on_time = sum(_completed_on_time(todo) for todo in due_completed)
    return {
        "completion_rate": _rate(len(completed), len(cohort)),
        "on_time_rate": _rate(on_time, len(due_completed)),
        "carryover_rate": _rate(len(cohort) - len(completed), len(cohort)),
    }, open_ids


def _goal_metrics(
    goals: dict[str, dict[str, Any]], week_start: date,
    previous: dict[str, Any] | None, state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    prior_goals = previous.get("goals", {}) if previous else {}
    if not isinstance(prior_goals, dict):
        raise ValueError("Personal weekly summary state has invalid goals")
    old_last = state.get("last_advanced", {})
    if not isinstance(old_last, dict):
        raise ValueError("Personal weekly summary state has invalid advancement dates")
    last_advanced = {
        goal_id: value for goal_id, value in old_last.items()
        if goal_id in goals and isinstance(value, str)
    }
    if previous is None:
        return {
            "goals_advanced": None, "goal_coverage": None,
            "new_goals": None, "new_subgoals": None,
            "advanced_goal_names": [], "new_goal_names": [], "new_subgoal_names": [],
        }, last_advanced
    old_child_ids = {
        child["id"] for goal in prior_goals.values() for child in goal.get("children", [])
    }
    new_child_ids = {
        child["id"] for goal in goals.values() for child in goal["children"]
    }
    progress_comparable = all(
        isinstance(goal, dict)
        and isinstance(goal.get("progress"), int)
        and not isinstance(goal["progress"], bool)
        and 0 <= goal["progress"] <= 100
        for goal in prior_goals.values()
    )
    if not progress_comparable:
        last_advanced = {}
    eligible = {
        goal_id for goal_id, prior_goal in prior_goals.items()
        if goal_id in goals and prior_goal.get("progress", 100) < 100
    } if progress_comparable else set()
    advanced = {
        goal_id for goal_id in eligible
        if goals[goal_id]["progress"] > prior_goals[goal_id]["progress"]
    }
    for goal_id in advanced:
        last_advanced[goal_id] = week_start.isoformat()
    new_goal_ids = goals.keys() - prior_goals.keys()
    new_children = [
        child for goal in goals.values() for child in goal["children"]
        if child["id"] not in old_child_ids
    ]
    return {
        "goals_advanced": len(advanced) if progress_comparable else None,
        "goal_coverage": _rate(len(advanced), len(eligible)) if progress_comparable else None,
        "new_goals": len(new_goal_ids),
        "new_subgoals": len(new_child_ids - old_child_ids),
        "advanced_goal_names": sorted(goals[goal_id]["title"] for goal_id in advanced),
        "new_goal_names": sorted(goals[goal_id]["title"] for goal_id in new_goal_ids),
        "new_subgoal_names": sorted(_short(child["title"]) for child in new_children),
    }, last_advanced


def _calendar_metrics(
    events: list[dict[str, Any]], start: date, end: date,
    active_start: int, active_end: int,
) -> dict[str, float]:
    week_start = _boundary(start, 0)
    week_end = _boundary(end, 0)
    intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        if event.get("status") not in {"confirmed", "tentative"}:
            raise ValueError("Google Calendar returned invalid event status")
        begin = event.get("start")
        finish = event.get("end")
        if not isinstance(begin, dict) or not isinstance(finish, dict):
            raise ValueError("Google Calendar returned invalid event times")
        if "date" in begin and "date" in finish:
            continue  # An all-day marker has no scheduled duration.
        if not isinstance(begin.get("date_time"), str) or not isinstance(finish.get("date_time"), str):
            raise ValueError("Google Calendar returned mixed event time forms")
        left = max(_timestamp(begin["date_time"]), week_start)
        right = min(_timestamp(finish["date_time"]), week_end)
        if right > left:
            intervals.append((left, right))
    merged = _merge(intervals)
    total = _hours(merged)
    occupied = 0.0
    longest = 0.0
    for offset in range(5):
        day = start + timedelta(days=offset)
        occupation_window = (_boundary(day, 9 * 60), _boundary(day + timedelta(days=1), 0))
        occupied += _hours(_intersect(merged, *occupation_window))
        active_window = (_boundary(day, active_start), _boundary(day, active_end))
        cursor = active_window[0]
        for left, right in _intersect(merged, *active_window):
            longest = max(longest, (left - cursor).total_seconds() / 3600)
            cursor = right
        longest = max(longest, (active_window[1] - cursor).total_seconds() / 3600)
    return {
        "scheduled_hours": round(total, 2),
        "occupation_rate": round(occupied / 75 * 100, 1),
        "longest_free_hours": round(longest, 2),
    }


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    return merged


def _intersect(
    intervals: list[tuple[datetime, datetime]], left: datetime, right: datetime,
) -> list[tuple[datetime, datetime]]:
    return [(max(a, left), min(b, right)) for a, b in intervals if b > left and a < right]


def _hours(intervals: list[tuple[datetime, datetime]]) -> float:
    return sum((right - left).total_seconds() for left, right in intervals) / 3600


def _history(state: dict[str, Any], week_start: date, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    prior = state.get("history", [])
    if not isinstance(prior, list):
        raise ValueError("Personal weekly summary state has invalid history")
    by_week = {
        row["week_start"]: row for row in prior
        if isinstance(row, dict) and isinstance(row.get("week_start"), str)
        and isinstance(row.get("metrics"), dict)
    }
    by_week[week_start.isoformat()] = {"week_start": week_start.isoformat(), "metrics": metrics}
    oldest_offset = min(
        3,
        max(((week_start - date.fromisoformat(key)).days // 7 for key in by_week if key != week_start.isoformat()), default=0),
    )
    return [
        by_week.get(
            (week_start - timedelta(days=offset * 7)).isoformat(),
            {"week_start": (week_start - timedelta(days=offset * 7)).isoformat(), "metrics": {}},
        )
        for offset in range(oldest_offset, -1, -1)
    ]


def _report_blocks(
    name: str, metrics: dict[str, Any], goals: dict[str, dict[str, Any]],
    last_advanced: dict[str, str], history: list[dict[str, Any]], comparable: bool,
) -> list[dict[str, Any]]:
    blocks = [_block("heading_1", name)]
    if not comparable:
        blocks.append(_block("paragraph", "First observation: weekly Todo and Goal comparisons begin after the next Sunday snapshot."))
    blocks.append(_block("heading_2", "Todos"))
    for key, label in (
        ("completion_rate", "Completion rate"), ("on_time_rate", "Observed on-time completion rate"),
        ("carryover_rate", "Carryover rate"),
    ):
        blocks.append(_metric_block(label, key, metrics, history))
    blocks.append(_block("heading_2", "Goals"))
    for key, label in (
        ("goals_advanced", "Goals advanced"), ("goal_coverage", "Goal coverage rate"),
        ("new_goals", "New goals"), ("new_subgoals", "New subgoals"),
    ):
        blocks.append(_metric_block(label, key, metrics, history))
    for key, label in (
        ("advanced_goal_names", "Advanced"),
        ("new_goal_names", "New goals"),
        ("new_subgoal_names", "New subgoals"),
    ):
        names = metrics[key]
        for index in range(0, len(names), 25):
            blocks.append(_block("paragraph", f"{label}: {'; '.join(names[index:index + 25])}"))
    week_start = date.fromisoformat(history[-1]["week_start"])
    goal_lines: list[str] = []
    for goal_id, goal in sorted(goals.items(), key=lambda pair: pair[1]["title"].casefold()):
        if goal["progress"] == 100:
            continue
        last = last_advanced.get(goal_id)
        weeks = (week_start - date.fromisoformat(last)).days // 7 if last else None
        prefix = f"{goal['title']} ({goal['progress']}%)"
        label = f"{prefix}: {weeks} week(s) since observed advancement" if weeks is not None else f"{prefix}: no observed advancement yet"
        goal_lines.append(label)
    for index in range(0, len(goal_lines), 10):
        blocks.append(_block("paragraph", "\n".join(goal_lines[index:index + 10])))
    blocks.append(_block("heading_2", "Calendar"))
    for key, label in (
        ("scheduled_hours", "Total scheduled hours"),
        ("occupation_rate", "Weekday 09:00–24:00 occupation rate"),
        ("longest_free_hours", "Longest weekday active-hours free block"),
    ):
        blocks.append(_metric_block(label, key, metrics, history))
    blocks.append(_block("paragraph", "Timed events are merged before counting; all-day events are excluded. Goal advancement means effective progress increased since the previous snapshot. Observed Todo on-time uses Notion's last edit time, which can be later than completion."))
    return blocks


def _metric_block(label: str, key: str, metrics: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    value = metrics[key]
    prior = history[-2]["metrics"].get(key) if len(history) > 1 else None
    suffix = "%" if key in RATE_KEYS else " h" if key.endswith("hours") else ""
    current_text = "N/A" if value is None else f"{value:g}{suffix}"
    delta = ""
    if value is not None and isinstance(prior, (int, float)):
        delta = f" ({value - prior:+g}{' pp' if key in RATE_KEYS else suffix} vs previous week)"
    values = [row.get("metrics", {}).get(key) for row in history]
    numeric = [number for number in values if isinstance(number, (int, float))]
    if numeric:
        low, high = min(numeric), max(numeric)
        graph = "".join(
            SPARK[round((number - low) / (high - low) * 7)] if isinstance(number, (int, float)) and high > low
            else SPARK[3] if isinstance(number, (int, float)) else "·"
            for number in values
        )
        points = ", ".join(
            f"{row['week_start'][5:]} {number:g}{suffix}" if isinstance(number, (int, float))
            else f"{row['week_start'][5:]} N/A"
            for row, number in zip(history, values, strict=True)
        )
        delta += f" · 4-week view {graph} ({points})"
    return _block("paragraph", f"{label}: {current_text}{delta}")


def _block(kind: str, value: str) -> dict[str, Any]:
    chunks = [value[index:index + 1900] for index in range(0, len(value), 1900)] or [""]
    return {"type": kind, kind: {"rich_text": [{"type": "text", "text": {"content": chunk}} for chunk in chunks]}}


def _clock(value: Any, *, allow_24: bool = False) -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":" or not value[:2].isdigit() or not value[3:].isdigit():
        raise ValueError("Active hours must use HH:MM")
    hour, minute = int(value[:2]), int(value[3:])
    if hour == 24 and minute == 0 and allow_24:
        return 1440
    if hour > 23 or minute > 59:
        raise ValueError("Active hours are invalid")
    return hour * 60 + minute


def _boundary(day: date, minutes: int) -> datetime:
    if minutes == 1440:
        day += timedelta(days=1)
        minutes = 0
    local = datetime.combine(day, time(minutes // 60, minutes % 60), ZONE)
    return local.astimezone(UTC)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A source timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("A source timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("A source timestamp is missing a timezone")
    return parsed.astimezone(UTC)


def _due_limit(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A Todo due date is invalid")
    if len(value) == 10:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise ValueError("A Todo due date is invalid") from None
        return _boundary(day + timedelta(days=1), 0)
    return _timestamp(value)


def _completed_on_time(todo: dict[str, Any]) -> bool:
    due = todo["due_at"]
    observed = _timestamp(todo["last_edited_at"])
    limit = _due_limit(due)
    return observed < limit if len(due) == 10 else observed <= limit


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def _short(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Atlas returned an invalid goal title")
    return value[:150]


def _cache_path() -> Path:
    configured = os.environ.get("PERSONAL_AGENT_SKILL_CACHE_DIR")
    return (Path(configured) if configured else Path("cache")) / STATE_FILE


def _load_state() -> dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Personal weekly summary cache is unreadable") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Personal weekly summary cache has an unsupported format")
    return value


def _save_state(value: dict[str, Any]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    payload = json.load(sys.stdin)
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
