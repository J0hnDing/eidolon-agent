import json
import sys
from typing import Any, Dict
from urllib.parse import quote

import requests


WTTR_BASE_URL = "https://wttr.in"
TIMEOUT_SECONDS = 10


class SkillError(Exception):
    """Controlled user-facing skill error."""


def _error(message: str) -> Dict[str, str]:
    return {"error": message}


def _validate_input(payload: Any) -> Dict[str, str]:
    if not isinstance(payload, dict):
        raise SkillError("Input must be a JSON object.")

    if set(payload) != {"city", "units"}:
        raise SkillError("Input must contain only city and units.")

    city = payload.get("city")
    units = payload.get("units")

    if not isinstance(city, str):
        raise SkillError("City must be a string.")

    city = city.strip()
    if not city:
        raise SkillError("City is required.")
    if len(city) > 120:
        raise SkillError("City must be 120 characters or fewer.")

    if units not in {"metric", "imperial"}:
        raise SkillError("Units must be either metric or imperial.")

    return {"city": city, "units": units}


def _fetch_weather(city: str) -> Dict[str, Any]:
    url = f"{WTTR_BASE_URL}/{quote(city)}"
    try:
        response = requests.get(
            url,
            params={"format": "j1"},
            timeout=TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        raise SkillError("Could not reach wttr.in for the current weather.") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise SkillError("wttr.in returned an unsuccessful weather response.")

    try:
        data = response.json()
    except ValueError as exc:
        raise SkillError("wttr.in returned invalid weather data.") from exc

    if not isinstance(data, dict):
        raise SkillError("wttr.in returned malformed weather data.")

    return data


def _first_condition(current: Dict[str, Any]) -> str:
    desc = current.get("weatherDesc")
    if isinstance(desc, list) and desc:
        first = desc[0]
        if isinstance(first, dict):
            value = first.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()

    code = current.get("weatherCode")
    if isinstance(code, str) and code.strip():
        return f"Weather code {code.strip()}"

    raise SkillError("Weather response is missing the current condition.")


def _parse_temperature(current: Dict[str, Any], units: str) -> float:
    field_name = "temp_C" if units == "metric" else "temp_F"
    value = current.get(field_name)

    if value is None and units == "imperial":
        metric_value = _number(current.get("temp_C"))
        return round((metric_value * 9 / 5) + 32, 1)

    return _number(value)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SkillError("Weather response is missing a valid current temperature.") from exc


def _current_weather(data: Dict[str, Any], units: str) -> Dict[str, Any]:
    current_list = data.get("current_condition")
    if not isinstance(current_list, list) or not current_list:
        raise SkillError("Weather response is missing current conditions.")

    current = current_list[0]
    if not isinstance(current, dict):
        raise SkillError("Weather response has malformed current conditions.")

    return {
        "temperature": _parse_temperature(current, units),
        "condition": _first_condition(current),
    }


def _condition_flags(condition: str) -> Dict[str, bool]:
    text = condition.lower()
    umbrella_words = (
        "rain",
        "shower",
        "drizzle",
        "thunder",
        "sleet",
        "snow",
        "hail",
    )
    severe_words = ("thunder", "storm", "snow", "sleet", "ice", "freezing", "hail")
    return {
        "umbrella": any(word in text for word in umbrella_words),
        "severe": any(word in text for word in severe_words),
    }


def _build_guidance(temperature: float, condition: str, units: str) -> Dict[str, Any]:
    flags = _condition_flags(condition)
    temp_c = temperature if units == "metric" else (temperature - 32) * 5 / 9

    jacket = temp_c < 18 or flags["severe"] or "wind" in condition.lower()

    if temp_c < 0:
        clothing = "Wear a winter coat, warm layers, and weather-safe shoes."
    elif temp_c < 10:
        clothing = "Wear a warm jacket, layers, and comfortable shoes."
    elif temp_c < 18:
        clothing = "Wear a light jacket or sweater with comfortable shoes."
    elif temp_c > 27:
        clothing = "Wear breathable clothing and comfortable shoes."
    else:
        clothing = "Wear comfortable layers and everyday shoes."

    if flags["umbrella"] and "umbrella" not in clothing.lower():
        recommendation = f"{clothing} Bring an umbrella."
    else:
        recommendation = clothing

    if flags["severe"]:
        commute_note = "Allow extra commute time and watch for slippery or stormy conditions."
    elif flags["umbrella"]:
        commute_note = "Allow extra time and bring an umbrella for the commute."
    elif temp_c > 27:
        commute_note = "Stay hydrated and choose a cooler route if possible."
    elif temp_c < 0:
        commute_note = "Leave extra time and watch for icy spots during the commute."
    else:
        commute_note = "Normal commute conditions; dress for the current temperature."

    return {
        "recommendation": recommendation,
        "umbrella": flags["umbrella"],
        "jacket": jacket,
        "commute_note": commute_note,
    }


def run(payload: Any) -> Dict[str, Any]:
    inputs = _validate_input(payload)
    weather_data = _fetch_weather(inputs["city"])
    current = _current_weather(weather_data, inputs["units"])
    guidance = _build_guidance(
        current["temperature"],
        current["condition"],
        inputs["units"],
    )

    temperature = current["temperature"]
    if temperature.is_integer():
        temperature = int(temperature)

    return {
        "recommendation": guidance["recommendation"],
        "umbrella": guidance["umbrella"],
        "jacket": guidance["jacket"],
        "temperature": temperature,
        "condition": current["condition"],
        "commute_note": guidance["commute_note"],
    }


def main() -> None:
    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input) if raw_input.strip() else {}
        result = run(payload)
    except json.JSONDecodeError:
        result = _error("Input must be valid JSON.")
    except SkillError as exc:
        result = _error(str(exc))
    except Exception:
        result = _error("Weather outfit planning failed unexpectedly.")

    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
