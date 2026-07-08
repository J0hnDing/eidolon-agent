import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skill.py"
MANIFEST_PATH = ROOT / "manifest.json"


class FakeRequestException(Exception):
    pass


class FakeResponse:
    def __init__(self, payload, status_code=200, json_error=None):
        self._payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


def weather_payload(temp_c="9", temp_f="48", condition="Light rain"):
    return {
        "current_condition": [
            {
                "temp_C": temp_c,
                "temp_F": temp_f,
                "weatherDesc": [{"value": condition}],
            }
        ]
    }


def load_skill(monkeypatch, response=None, request_error=None):
    calls = []
    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = FakeRequestException

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if request_error:
            raise request_error
        return response if response is not None else FakeResponse(weather_payload())

    fake_requests.get = fake_get
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    module_name = "weather_outfit_skill_under_test"
    spec = importlib.util.spec_from_file_location(module_name, SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, calls


def test_manifest_contract_supports_fetch_logic_and_tool_ui():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["name"] == "weather_outfit_planner"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["dependencies"] == ["requests"]
    assert manifest["permissions"]["network"] == ["wttr.in"]
    assert manifest["permissions"]["filesystem_read"] == []
    assert manifest["permissions"]["filesystem_write"] == []
    assert manifest["permissions"]["secrets"] == []
    assert manifest["permissions"]["shell"] is False

    input_schema = manifest["input_schema"]
    assert input_schema["required"] == ["city", "units"]
    assert input_schema["additionalProperties"] is False
    assert input_schema["properties"]["city"]["minLength"] == 1
    assert input_schema["properties"]["city"]["maxLength"] == 120
    assert input_schema["properties"]["units"]["enum"] == ["metric", "imperial"]

    output_schema = manifest["output_schema"]
    assert output_schema["additionalProperties"] is False
    assert set(output_schema["required"]) == {
        "recommendation",
        "umbrella",
        "jacket",
        "temperature",
        "condition",
        "commute_note",
    }
    assert output_schema["properties"]["umbrella"]["type"] == "boolean"
    assert output_schema["properties"]["jacket"]["type"] == "boolean"
    assert output_schema["properties"]["temperature"]["type"] == "number"

    ui_schema = manifest["tool_ui_schema"]
    assert ui_schema["submit_label"] == "Plan Outfit"
    assert {field["name"] for field in ui_schema["fields"]} == set(input_schema["properties"])
    assert ui_schema["result_template"]["primary_field"] == "recommendation"
    result_fields = {field["field"] for field in ui_schema["result_template"]["secondary_fields"]}
    assert result_fields == set(output_schema["properties"]) - {"recommendation"}


def test_metric_rainy_weather_normalizes_city_and_returns_commute_flags(monkeypatch):
    skill, calls = load_skill(
        monkeypatch,
        response=FakeResponse(weather_payload(temp_c="7", temp_f="45", condition="Light rain")),
    )

    result = skill.run({"city": "  New York  ", "units": "metric"})

    assert calls[0]["url"] == "https://wttr.in/New%20York"
    assert calls[0]["params"] == {"format": "j1"}
    assert calls[0]["timeout"] == 10
    assert calls[0]["headers"]["Accept"] == "application/json"
    assert result == {
        "recommendation": "Wear a warm jacket, layers, and comfortable shoes. Bring an umbrella.",
        "umbrella": True,
        "jacket": True,
        "temperature": 7,
        "condition": "Light rain",
        "commute_note": "Allow extra time and bring an umbrella for the commute.",
    }


def test_imperial_warm_clear_weather_uses_fahrenheit_field(monkeypatch):
    skill, calls = load_skill(
        monkeypatch,
        response=FakeResponse(weather_payload(temp_c="31", temp_f="88", condition="Sunny")),
    )

    result = skill.run({"city": "Phoenix", "units": "imperial"})

    assert calls[0]["url"] == "https://wttr.in/Phoenix"
    assert result["temperature"] == 88
    assert result["condition"] == "Sunny"
    assert result["umbrella"] is False
    assert result["jacket"] is False
    assert result["recommendation"] == "Wear breathable clothing and comfortable shoes."
    assert result["commute_note"] == "Stay hydrated and choose a cooler route if possible."


def test_cold_and_severe_weather_are_deterministic(monkeypatch):
    skill, _ = load_skill(
        monkeypatch,
        response=FakeResponse(weather_payload(temp_c="-4", temp_f="25", condition="Blowing snow")),
    )

    result = skill.run({"city": "Montreal", "units": "metric"})

    assert result["temperature"] == -4
    assert result["umbrella"] is True
    assert result["jacket"] is True
    assert result["recommendation"] == "Wear a winter coat, warm layers, and weather-safe shoes. Bring an umbrella."
    assert result["commute_note"] == "Allow extra commute time and watch for slippery or stormy conditions."


def test_invalid_inputs_and_weather_failures_return_controlled_errors(monkeypatch):
    skill, _ = load_skill(monkeypatch)

    invalid_cases = [
        ({}, "Input must contain only city and units."),
        ({"city": "   ", "units": "metric"}, "City is required."),
        ({"city": "Toronto", "units": "kelvin"}, "Units must be either metric or imperial."),
        ({"city": "Toronto", "units": "metric", "extra": True}, "Input must contain only city and units."),
    ]
    for payload, message in invalid_cases:
        try:
            skill.run(payload)
        except skill.SkillError as exc:
            assert str(exc) == message
        else:
            raise AssertionError(f"Expected SkillError for {payload!r}")

    failure_cases = [
        (FakeResponse(weather_payload(), status_code=500), "wttr.in returned an unsuccessful weather response."),
        (FakeResponse({}, status_code=200), "Weather response is missing current conditions."),
        (FakeResponse({"current_condition": [{}]}, status_code=200), "Weather response is missing a valid current temperature."),
        (FakeResponse(weather_payload(temp_c="bad"), status_code=200), "Weather response is missing a valid current temperature."),
        (FakeResponse(None, status_code=200, json_error=ValueError("bad json")), "wttr.in returned invalid weather data."),
    ]
    for response, message in failure_cases:
        skill, _ = load_skill(monkeypatch, response=response)
        try:
            skill.run({"city": "Toronto", "units": "metric"})
        except skill.SkillError as exc:
            assert str(exc) == message
        else:
            raise AssertionError(f"Expected SkillError: {message}")

    skill, _ = load_skill(monkeypatch, request_error=FakeRequestException("offline"))
    try:
        skill.run({"city": "Toronto", "units": "metric"})
    except skill.SkillError as exc:
        assert str(exc) == "Could not reach wttr.in for the current weather."
    else:
        raise AssertionError("Expected controlled network SkillError")


def test_json_stdin_stdout_success_uses_fake_requests_without_live_network(tmp_path):
    fake_requests = tmp_path / "requests.py"
    fake_requests.write_text(
        """
class RequestException(Exception):
    pass

class Response:
    status_code = 200

    def json(self):
        return {
            "current_condition": [
                {
                    "temp_C": "16",
                    "temp_F": "61",
                    "weatherDesc": [{"value": "Partly cloudy"}],
                }
            ]
        }

def get(url, **kwargs):
    assert url == "https://wttr.in/Toronto"
    assert kwargs["params"] == {"format": "j1"}
    return Response()
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps({"city": "Toronto", "units": "metric"}),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )

    output = json.loads(completed.stdout)
    assert completed.stderr == ""
    assert output["temperature"] == 16
    assert output["condition"] == "Partly cloudy"
    assert output["umbrella"] is False
    assert output["jacket"] is True
    assert set(output) == {
        "recommendation",
        "umbrella",
        "jacket",
        "temperature",
        "condition",
        "commute_note",
    }
