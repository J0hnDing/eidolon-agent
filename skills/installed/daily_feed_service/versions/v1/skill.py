from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener
from zoneinfo import ZoneInfo

import integration_runtime_capabilities

TIMEZONE = ZoneInfo("America/Toronto")
LATITUDE = 43.6532
LONGITUDE = -79.3832
LOCATION_NAME = "Toronto"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
CALENDAR_OPERATION = "google_calendar.event.list"
TODO_OPERATION = "notion.todo.list"
DAILY_FEED_OPERATION = "notion.daily_feed.write"
MAX_PROVIDER_PAGES = 100
MAX_RENDERED_ITEMS = 30
MAX_TEXT = 160
MAX_WEATHER_BYTES = 256 * 1024
MAX_MARKDOWN = 20_000

WEATHER_CODES = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Light freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light rain showers",
    81: "Rain showers",
    82: "Heavy rain showers",
    85: "Light snow showers",
    86: "Snow showers",
    95: "Thunderstorms",
    96: "Thunderstorms with light hail",
    99: "Thunderstorms with hail",
}


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise ValueError("Open-Meteo redirected the fixed forecast request")


def run(
    input_json: dict[str, Any],
    *,
    now: datetime | None = None,
    weather_fetcher: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if input_json:
        raise ValueError("Daily Feed service input must be an empty object")
    local_now = _local_now(now)
    weather = _validated_weather((weather_fetcher or _fetch_weather)(), local_now.date())
    events = _calendar_events(local_now)
    todos_by_date = _todos_due(local_now.date())
    markdown = _feed_markdown(local_now, weather, events, todos_by_date)
    if len(markdown) > MAX_MARKDOWN:
        raise ValueError("Daily Feed markdown exceeded the integration limit")
    result = integration_runtime_capabilities.call(
        operation=DAILY_FEED_OPERATION,
        input={"markdown": markdown},
    )
    if result != {"updated": True, "characters": len(markdown)}:
        raise ValueError("Notion Daily Feed write returned an invalid result")
    return {
        "date": local_now.date().isoformat(),
        "page_updated": True,
        "characters": len(markdown),
        "event_count": len(events),
        "todo_count": sum(len(items) for items in todos_by_date.values()),
    }


def _local_now(value: datetime | None) -> datetime:
    value = value or datetime.now(TIMEZONE)
    if value.tzinfo is None:
        raise ValueError("Daily Feed current time must include a timezone")
    return value.astimezone(TIMEZONE)


def _fetch_weather() -> dict[str, Any]:
    query = urlencode(
        {
            "latitude": LATITUDE,
            "longitude": LONGITUDE,
            "daily": ",".join(
                (
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "apparent_temperature_min",
                    "precipitation_probability_max",
                    "precipitation_sum",
                    "snowfall_sum",
                    "wind_speed_10m_max",
                )
            ),
            "timezone": str(TIMEZONE),
            "forecast_days": 1,
        }
    )
    opener = build_opener(_RejectRedirects())
    with opener.open(f"{WEATHER_URL}?{query}", timeout=10) as response:
        if response.geturl().split("?", 1)[0] != WEATHER_URL:
            raise ValueError("Open-Meteo returned an unexpected response URL")
        raw = response.read(MAX_WEATHER_BYTES + 1)
    if len(raw) > MAX_WEATHER_BYTES:
        raise ValueError("Open-Meteo response exceeded the service limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Open-Meteo returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Open-Meteo returned an invalid response")
    return value


def _validated_weather(payload: dict[str, Any], expected_date: date) -> dict[str, float | int]:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise ValueError("Open-Meteo response is missing daily weather")
    fields = (
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "apparent_temperature_max",
        "apparent_temperature_min",
        "precipitation_probability_max",
        "precipitation_sum",
        "snowfall_sum",
        "wind_speed_10m_max",
    )
    times = daily.get("time")
    if times != [expected_date.isoformat()]:
        raise ValueError("Open-Meteo returned weather for the wrong date")
    result: dict[str, float | int] = {}
    for field in fields:
        values = daily.get(field)
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError(f"Open-Meteo returned an invalid {field}")
        value = values[0]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"Open-Meteo returned an invalid {field}")
        result[field] = int(value) if field == "weather_code" else float(value)
    if not 0 <= result["precipitation_probability_max"] <= 100:
        raise ValueError("Open-Meteo returned an invalid precipitation probability")
    if result["precipitation_sum"] < 0 or result["snowfall_sum"] < 0:
        raise ValueError("Open-Meteo returned invalid precipitation totals")
    return result


def _calendar_events(local_now: datetime) -> list[dict[str, Any]]:
    start = datetime.combine(local_now.date(), time.min, tzinfo=TIMEZONE)
    end = start + timedelta(days=1)
    input_json: dict[str, Any] = {
        "time_min": start.isoformat(),
        "time_max": end.isoformat(),
        "page_size": 100,
    }
    events: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for _ in range(MAX_PROVIDER_PAGES):
        page = integration_runtime_capabilities.call(
            operation=CALENDAR_OPERATION,
            input=input_json,
        )
        page_events, has_more, token, effective_min, effective_max = _validated_calendar_page(page)
        events.extend(page_events)
        if not has_more:
            return sorted(events, key=_event_sort_key)
        if token is None or token in seen_tokens:
            raise ValueError("Google Calendar returned invalid pagination")
        seen_tokens.add(token)
        input_json = {
            "time_min": effective_min,
            "page_size": 100,
            "page_token": token,
        }
        if effective_max is not None:
            input_json["time_max"] = effective_max
    raise ValueError("Google Calendar pagination exceeded the service limit")


def _validated_calendar_page(
    page: Any,
) -> tuple[list[dict[str, Any]], bool, str | None, str, str | None]:
    if not isinstance(page, dict):
        raise ValueError("Google Calendar returned an invalid page")
    events = page.get("events")
    has_more = page.get("has_more")
    token = page.get("next_page_token")
    effective_min = page.get("time_min")
    effective_max = page.get("time_max")
    if (
        not isinstance(events, list)
        or len(events) > 100
        or not all(isinstance(event, dict) for event in events)
        or not isinstance(has_more, bool)
        or (token is not None and not isinstance(token, str))
        or not isinstance(effective_min, str)
        or (effective_max is not None and not isinstance(effective_max, str))
    ):
        raise ValueError("Google Calendar returned invalid pagination data")
    for event in events:
        _event_sort_key(event)
        summary = event.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("Google Calendar returned an invalid event title")
    return events, has_more, token, effective_min, effective_max


def _event_sort_key(event: dict[str, Any]) -> tuple[datetime, str, str]:
    start = event.get("start")
    if not isinstance(start, dict):
        raise ValueError("Google Calendar returned an invalid event start")
    if isinstance(start.get("date_time"), str):
        moment = _parse_datetime(start["date_time"], "event start").astimezone(TIMEZONE)
        kind = "1"
    elif isinstance(start.get("date"), str):
        try:
            day = date.fromisoformat(start["date"])
        except ValueError:
            raise ValueError("Google Calendar returned an invalid all-day event") from None
        moment = datetime.combine(day, time.min, tzinfo=TIMEZONE)
        kind = "0"
    else:
        raise ValueError("Google Calendar returned an invalid event start")
    summary = event.get("summary")
    event_id = event.get("id")
    return moment, kind, f"{summary or ''}\0{event_id or ''}".casefold()


def _todos_due(start_date: date) -> dict[date, list[dict[str, Any]]]:
    target_dates = [start_date + timedelta(days=offset) for offset in range(3)]
    result = {target: [] for target in target_dates}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(MAX_PROVIDER_PAGES):
        input_json: dict[str, Any] = {"page_size": 100}
        if cursor is not None:
            input_json["start_cursor"] = cursor
        page = integration_runtime_capabilities.call(
            operation=TODO_OPERATION,
            input=input_json,
        )
        todos, has_more, next_cursor = _validated_todo_page(page)
        for todo in todos:
            if todo["done"]:
                continue
            due_at = todo["due_at"]
            if due_at is None:
                continue
            due_date, due_moment = _todo_due_value(due_at)
            if due_date in result:
                result[due_date].append({**todo, "_due_moment": due_moment})
        if not has_more:
            for items in result.values():
                items.sort(key=_todo_sort_key)
            return result
        if next_cursor is None or next_cursor in seen_cursors:
            raise ValueError("Notion Todo returned invalid pagination")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise ValueError("Notion Todo pagination exceeded the service limit")


def _validated_todo_page(page: Any) -> tuple[list[dict[str, Any]], bool, str | None]:
    if not isinstance(page, dict):
        raise ValueError("Notion Todo returned an invalid page")
    todos = page.get("todos")
    has_more = page.get("has_more")
    cursor = page.get("next_cursor")
    if (
        not isinstance(todos, list)
        or len(todos) > 100
        or not isinstance(has_more, bool)
        or (cursor is not None and not isinstance(cursor, str))
    ):
        raise ValueError("Notion Todo returned invalid pagination data")
    for todo in todos:
        if (
            not isinstance(todo, dict)
            or not isinstance(todo.get("title"), str)
            or not isinstance(todo.get("done"), bool)
            or (todo.get("due_at") is not None and not isinstance(todo.get("due_at"), str))
            or todo.get("priority") not in {None, "low", "medium", "high"}
        ):
            raise ValueError("Notion Todo returned an invalid todo")
    return todos, has_more, cursor


def _todo_due_value(value: str) -> tuple[date, datetime | None]:
    if len(value) == 10:
        try:
            return date.fromisoformat(value), None
        except ValueError:
            raise ValueError("Notion Todo returned an invalid due date") from None
    moment = _parse_datetime(value, "todo due time").astimezone(TIMEZONE)
    return moment.date(), moment


def _todo_sort_key(todo: dict[str, Any]) -> tuple[datetime, int, str]:
    due = todo["_due_moment"] or datetime.combine(
        _todo_due_value(todo["due_at"])[0], time.max, tzinfo=TIMEZONE
    )
    priority = {"high": 0, "medium": 1, "low": 2, None: 3}[todo["priority"]]
    return due, priority, todo["title"].casefold()


def _parse_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Provider returned an invalid {label}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"Provider returned a timezone-free {label}")
    return parsed


def _feed_markdown(
    local_now: datetime,
    weather: dict[str, float | int],
    events: list[dict[str, Any]],
    todos_by_date: dict[date, list[dict[str, Any]]],
) -> str:
    lines = [
        f"# Daily Feed — {local_now.strftime('%A, %B')} {local_now.day}, {local_now.year}",
        "",
        "## Weather",
        _weather_summary(weather),
        f"Wear: {_clothing_recommendation(weather)}",
        "",
        "## Schedule",
    ]
    lines.extend(_event_lines(events))
    lines.extend(["", "## Todos"])
    labels = ("Today", "Tomorrow", "Day after tomorrow")
    for label, target in zip(labels, todos_by_date, strict=True):
        lines.extend(["", f"### {label}"])
        lines.extend(_todo_lines(todos_by_date[target]))
    return "\n".join(lines).strip() + "\n"


def _weather_summary(weather: dict[str, float | int]) -> str:
    code = int(weather["weather_code"])
    condition = WEATHER_CODES.get(code, "Unsettled")
    low = round(float(weather["temperature_2m_min"]))
    high = round(float(weather["temperature_2m_max"]))
    feels_low = round(float(weather["apparent_temperature_min"]))
    feels_high = round(float(weather["apparent_temperature_max"]))
    rain = round(float(weather["precipitation_probability_max"]))
    wind = round(float(weather["wind_speed_10m_max"]))
    return (
        f"{LOCATION_NAME}: {condition.lower()}. {low}–{high}°C "
        f"(feels {feels_low}–{feels_high}°C); rain {rain}%; wind up to {wind} km/h."
    )


def _clothing_recommendation(weather: dict[str, float | int]) -> str:
    feels_low = float(weather["apparent_temperature_min"])
    feels_high = float(weather["apparent_temperature_max"])
    rain_probability = float(weather["precipitation_probability_max"])
    precipitation = float(weather["precipitation_sum"])
    snowfall = float(weather["snowfall_sum"])
    wind = float(weather["wind_speed_10m_max"])
    code = int(weather["weather_code"])
    if feels_low < -10:
        items = ["parka", "thermal layers", "hat and gloves", "insulated boots"]
    elif feels_low < 0:
        items = ["winter coat", "warm layers", "hat and gloves"]
    elif feels_low < 8:
        items = ["warm coat or layered jacket", "long pants"]
    elif feels_low < 15:
        items = ["light jacket or sweater", "long pants"]
    elif feels_low < 22:
        items = ["light layers", "a light jacket for cooler hours"]
    else:
        items = ["light, breathable clothing"]
    if feels_high >= 28:
        items.extend(["sun hat", "water"])
    if rain_probability >= 40 or precipitation >= 1 or code in range(51, 68) or code in range(80, 83):
        items.extend(["waterproof layer", "umbrella"])
    if snowfall > 0 or code in {71, 73, 75, 77, 85, 86}:
        items.append("waterproof boots")
    if wind >= 35:
        items.append("wind-resistant outer layer")
    return ", ".join(_deduplicate(items)) + "."


def _event_lines(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return ["- No events."]
    lines = []
    for event in events[:MAX_RENDERED_ITEMS]:
        start = event["start"]
        end = event.get("end")
        title = _plain(event.get("summary") or "Untitled event")
        if "date" in start:
            label = "All day"
        else:
            start_time = _parse_datetime(start["date_time"], "event start").astimezone(TIMEZONE)
            if isinstance(end, dict) and isinstance(end.get("date_time"), str):
                end_time = _parse_datetime(end["date_time"], "event end").astimezone(TIMEZONE)
                label = f"{start_time:%H:%M}–{end_time:%H:%M}"
            else:
                label = f"{start_time:%H:%M}"
        lines.append(f"- {label} — {title}")
    if len(events) > MAX_RENDERED_ITEMS:
        lines.append(f"- …and {len(events) - MAX_RENDERED_ITEMS} more events.")
    return lines


def _todo_lines(todos: list[dict[str, Any]]) -> list[str]:
    if not todos:
        return ["- None."]
    lines = []
    for todo in todos[:MAX_RENDERED_ITEMS]:
        due = todo["_due_moment"]
        prefix = f"{due:%H:%M} — " if due is not None else ""
        priority = f" [{todo['priority']}]" if todo["priority"] is not None else ""
        lines.append(f"- {prefix}{_plain(todo['title'])}{priority}")
    if len(todos) > MAX_RENDERED_ITEMS:
        lines.append(f"- …and {len(todos) - MAX_RENDERED_ITEMS} more todos.")
    return lines


def _plain(value: str) -> str:
    collapsed = " ".join(value.split())[:MAX_TEXT]
    escaped = collapsed.replace("\\", "\\\\")
    for character in "*_[]`#":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped or "Untitled"


def _deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def main() -> None:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Daily Feed service input must be a JSON object")
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
