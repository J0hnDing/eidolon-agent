from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
for parent in Path(__file__).resolve().parents:
    backend_dir = parent / "backend"
    if (backend_dir / "integration_runtime_capabilities.py").is_file():
        sys.path.insert(0, str(backend_dir))
        break
sys.path.insert(0, str(SKILL_DIR))

import skill  # noqa: E402

TORONTO = ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 17, 8, 30, tzinfo=TORONTO)


def _weather(**overrides):
    values = {
        "weather_code": 2,
        "temperature_2m_max": 20.2,
        "temperature_2m_min": 11.6,
        "apparent_temperature_max": 19.1,
        "apparent_temperature_min": 9.4,
        "precipitation_probability_max": 45,
        "precipitation_sum": 1.2,
        "snowfall_sum": 0,
        "wind_speed_10m_max": 22.4,
    }
    values.update(overrides)
    return {
        "daily": {
            "time": ["2026-09-17"],
            **{key: [value] for key, value in values.items()},
        }
    }


def _calendar_page(events=None, *, has_more=False, token=None):
    return {
        "events": [] if events is None else events,
        "time_min": "2026-09-17T00:00:00-04:00",
        "time_max": "2026-09-18T00:00:00-04:00",
        "has_more": has_more,
        "next_page_token": token,
    }


def _event(title="Lecture", start="2026-09-17T09:00:00-04:00", end="2026-09-17T10:00:00-04:00"):
    return {
        "id": title.casefold(),
        "summary": title,
        "start": {"date_time": start, "time_zone": "America/Toronto"},
        "end": {"date_time": end, "time_zone": "America/Toronto"},
    }


def _todo(title: str, due_at: str, *, priority=None, done=False):
    return {
        "id": title.casefold(),
        "title": title,
        "done": done,
        "priority": priority,
        "due_at": due_at,
    }


def _todo_page(todos=None, *, has_more=False, cursor=None):
    return {
        "todos": [] if todos is None else todos,
        "has_more": has_more,
        "next_cursor": cursor,
    }


def test_run_writes_one_concise_feed_with_weather_schedule_and_three_todo_days(monkeypatch):
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        if kwargs["operation"] == skill.CALENDAR_OPERATION:
            return _calendar_page(
                [
                    _event("Afternoon lab", "2026-09-17T14:00:00-04:00", "2026-09-17T16:00:00-04:00"),
                    {
                        "id": "all-day",
                        "summary": "Campus day",
                        "start": {"date": "2026-09-17"},
                        "end": {"date": "2026-09-18"},
                    },
                ]
            )
        if kwargs["operation"] == skill.TODO_OPERATION:
            return _todo_page(
                [
                    _todo("Submit *problem set*", "2026-09-17T23:59:00-04:00", priority="high"),
                    _todo("Read chapter", "2026-09-18"),
                    _todo("Prepare notes", "2026-09-19", priority="medium"),
                    _todo("Already done", "2026-09-17", done=True),
                    _todo("Later", "2026-09-20"),
                ]
            )
        markdown = kwargs["input"]["markdown"]
        return {"updated": True, "characters": len(markdown)}

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", invoke)
    result = skill.run({}, now=NOW, weather_fetcher=_weather)

    assert [call["operation"] for call in calls] == [
        skill.CALENDAR_OPERATION,
        skill.TODO_OPERATION,
        skill.DAILY_FEED_OPERATION,
    ]
    markdown = calls[-1]["input"]["markdown"]
    assert "# Daily Feed — Thursday, September 17, 2026" in markdown
    assert "Toronto: partly cloudy. 12–20°C (feels 9–19°C); rain 45%; wind up to 22 km/h." in markdown
    assert "Wear: light jacket or sweater, long pants, waterproof layer, umbrella." in markdown
    assert markdown.index("All day — Campus day") < markdown.index("14:00–16:00 — Afternoon lab")
    assert "23:59 — Submit \\*problem set\\* [high]" in markdown
    assert "### Tomorrow\n- Read chapter" in markdown
    assert "### Day after tomorrow\n- Prepare notes [medium]" in markdown
    assert "Already done" not in markdown
    assert "Later" not in markdown
    assert len(markdown) < 2_000
    assert result == {
        "date": "2026-09-17",
        "page_updated": True,
        "characters": len(markdown),
        "event_count": 2,
        "todo_count": 3,
    }


def test_calendar_and_todo_pagination_are_followed_with_stable_bounds(monkeypatch):
    calendar_pages = iter(
        [
            _calendar_page([_event("First")], has_more=True, token="cal-2"),
            _calendar_page([_event("Second", "2026-09-17T11:00:00-04:00", "2026-09-17T12:00:00-04:00")]),
        ]
    )
    todo_pages = iter(
        [
            _todo_page([_todo("Today", "2026-09-17")], has_more=True, cursor="todo-2"),
            _todo_page([_todo("Tomorrow", "2026-09-18")]),
        ]
    )
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        if kwargs["operation"] == skill.CALENDAR_OPERATION:
            return next(calendar_pages)
        if kwargs["operation"] == skill.TODO_OPERATION:
            return next(todo_pages)
        markdown = kwargs["input"]["markdown"]
        return {"updated": True, "characters": len(markdown)}

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", invoke)
    result = skill.run({}, now=NOW, weather_fetcher=_weather)

    calendar_calls = [call for call in calls if call["operation"] == skill.CALENDAR_OPERATION]
    assert calendar_calls[1]["input"] == {
        "time_min": "2026-09-17T00:00:00-04:00",
        "time_max": "2026-09-18T00:00:00-04:00",
        "page_size": 100,
        "page_token": "cal-2",
    }
    todo_calls = [call for call in calls if call["operation"] == skill.TODO_OPERATION]
    assert todo_calls[1]["input"] == {"page_size": 100, "start_cursor": "todo-2"}
    assert result["event_count"] == 2
    assert result["todo_count"] == 2


@pytest.mark.parametrize(
    ("weather", "expected"),
    [
        (
            _weather(apparent_temperature_min=-12, precipitation_probability_max=0, precipitation_sum=0),
            "parka, thermal layers, hat and gloves, insulated boots.",
        ),
        (
            _weather(
                weather_code=73,
                apparent_temperature_min=2,
                snowfall_sum=4,
                wind_speed_10m_max=40,
                precipitation_probability_max=0,
                precipitation_sum=0,
            ),
            "warm coat or layered jacket, long pants, waterproof boots, wind-resistant outer layer.",
        ),
        (
            _weather(
                apparent_temperature_min=23,
                apparent_temperature_max=31,
                precipitation_probability_max=0,
                precipitation_sum=0,
            ),
            "light, breathable clothing, sun hat, water.",
        ),
    ],
)
def test_clothing_recommendations_are_deterministic(weather, expected):
    validated = skill._validated_weather(weather, NOW.date())
    assert skill._clothing_recommendation(validated) == expected


def test_empty_schedule_and_todos_are_explicit(monkeypatch):
    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        if kwargs["operation"] == skill.CALENDAR_OPERATION:
            return _calendar_page()
        if kwargs["operation"] == skill.TODO_OPERATION:
            return _todo_page()
        markdown = kwargs["input"]["markdown"]
        return {"updated": True, "characters": len(markdown)}

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", invoke)
    skill.run({}, now=NOW, weather_fetcher=lambda: _weather(precipitation_probability_max=0, precipitation_sum=0))

    markdown = calls[-1]["input"]["markdown"]
    assert "## Schedule\n- No events." in markdown
    assert markdown.count("- None.") == 3


def test_invalid_weather_or_write_result_fails(monkeypatch):
    with pytest.raises(ValueError, match="wrong date"):
        skill._validated_weather(
            {**_weather(), "daily": {**_weather()["daily"], "time": ["2026-09-18"]}},
            NOW.date(),
        )

    def invoke(**kwargs):
        if kwargs["operation"] == skill.CALENDAR_OPERATION:
            return _calendar_page()
        if kwargs["operation"] == skill.TODO_OPERATION:
            return _todo_page()
        return {"updated": True, "characters": 0}

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", invoke)
    with pytest.raises(ValueError, match="invalid result"):
        skill.run({}, now=NOW, weather_fetcher=_weather)


def test_manifest_has_one_paused_install_schedule_and_no_llm_surface():
    manifest = json.loads((SKILL_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime"] == "service"
    assert manifest["schedule"] == {
        "type": "daily",
        "time": "08:30",
        "timezone": "America/Toronto",
        "input": {},
    }
    assert manifest["permissions"]["network"] == ["api.open-meteo.com"]
    assert manifest["permissions"]["codex"] == {
        "call_response": False,
        "internet_access": False,
    }
    assert not hasattr(skill, "call_codex")
    with pytest.raises(ValueError, match="empty object"):
        skill.run({"unexpected": True}, now=NOW, weather_fetcher=_weather)
