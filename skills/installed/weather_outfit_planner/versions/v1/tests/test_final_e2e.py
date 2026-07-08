import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
SKILL_PATH = ROOT / "skill.py"


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class FakeRequestException(Exception):
    pass


def load_skill(monkeypatch, response=None, request_error=None):
    calls = []
    fake_requests = types.ModuleType("requests")
    fake_requests.RequestException = FakeRequestException

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if request_error:
            raise request_error
        return response if response is not None else MockResponse(wttr_payload())

    fake_requests.get = fake_get
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    spec = importlib.util.spec_from_file_location("weather_outfit_planner_skill", SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, calls


class MockResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def wttr_payload(temp_c="21", temp_f="70", condition="Clear"):
    return {
        "current_condition": [
            {
                "temp_C": temp_c,
                "temp_F": temp_f,
                "weatherDesc": [{"value": condition}],
            }
        ]
    }


def assert_output_contract(result):
    assert set(result) == {
        "recommendation",
        "umbrella",
        "jacket",
        "temperature",
        "condition",
        "commute_note",
    }
    assert isinstance(result["recommendation"], str)
    assert isinstance(result["umbrella"], bool)
    assert isinstance(result["jacket"], bool)
    assert isinstance(result["temperature"], (int, float))
    assert isinstance(result["condition"], str)
    assert isinstance(result["commute_note"], str)


def test_manifest_contract_permissions_and_tool_ui_schema_align():
    manifest = load_manifest()

    assert manifest["name"] == "weather_outfit_planner"
    assert manifest["skill_type"] == "automation"
    assert manifest["interface_type"] == "tool"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["dependencies"] == ["requests"]
    assert manifest["permissions"] == {
        "network": ["wttr.in"],
        "filesystem_read": [],
        "filesystem_write": [],
        "secrets": [],
        "shell": False,
    }
    for blocked in (
        "browser_automation",
        "public_posting",
        "purchases",
        "trading",
        "file_deletion",
    ):
        assert blocked not in manifest["permissions"]

    input_schema = manifest["input_schema"]
    assert input_schema["additionalProperties"] is False
    assert input_schema["required"] == ["city", "units"]
    assert input_schema["properties"]["city"]["minLength"] == 1
    assert input_schema["properties"]["city"]["maxLength"] == 120
    assert input_schema["properties"]["units"]["enum"] == ["metric", "imperial"]

    output_schema = manifest["output_schema"]
    assert output_schema["additionalProperties"] is False
    assert output_schema["required"] == [
        "recommendation",
        "umbrella",
        "jacket",
        "temperature",
        "condition",
        "commute_note",
    ]

    ui_schema = manifest["tool_ui_schema"]
    assert ui_schema["title"] == "Weather Outfit Planner"
    assert ui_schema["submit_label"] == "Plan Outfit"
    assert ui_schema["fields"]
    assert {field["name"] for field in ui_schema["fields"]} == set(input_schema["properties"])
    assert ui_schema["result_template"]["primary_field"] == "recommendation"
    result_fields = {item["field"] for item in ui_schema["result_template"]["secondary_fields"]}
    assert result_fields | {"recommendation"} == set(output_schema["properties"])


@pytest.mark.parametrize(
    ("payload", "weather", "expected"),
    [
        (
            {"city": " Toronto ", "units": "metric"},
            wttr_payload(temp_c="8", temp_f="46", condition="Light rain"),
            {"temperature": 8, "umbrella": True, "jacket": True, "condition": "Light rain"},
        ),
        (
            {"city": "Phoenix", "units": "imperial"},
            wttr_payload(temp_c="34", temp_f="93", condition="Sunny"),
            {"temperature": 93, "umbrella": False, "jacket": False, "condition": "Sunny"},
        ),
    ],
)
def test_run_returns_deterministic_outfit_guidance_with_mocked_wttr(monkeypatch, payload, weather, expected):
    skill, calls = load_skill(monkeypatch, response=MockResponse(weather))

    result = skill.run(payload)

    assert_output_contract(result)
    assert result["temperature"] == expected["temperature"]
    assert result["umbrella"] is expected["umbrella"]
    assert result["jacket"] is expected["jacket"]
    assert result["condition"] == expected["condition"]
    assert len(result["recommendation"]) <= 95
    assert len(result["commute_note"]) <= 85
    assert calls == [
        {
            "url": f"https://wttr.in/{payload['city'].strip()}",
            "params": {"format": "j1"},
            "timeout": 10,
            "headers": {"Accept": "application/json"},
        }
    ]


def test_imperial_path_converts_metric_temperature_when_temp_f_missing(monkeypatch):
    skill, _ = load_skill(
        monkeypatch,
        response=MockResponse(wttr_payload(temp_c="0", temp_f=None, condition="Snow")),
    )

    result = skill.run({"city": "Montreal", "units": "imperial"})

    assert_output_contract(result)
    assert result["temperature"] == 32
    assert result["umbrella"] is True
    assert result["jacket"] is True
    assert "extra" in result["commute_note"].lower()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (MockResponse({}, status_code=503), "unsuccessful"),
        (MockResponse({"current_condition": []}), "missing current conditions"),
        (MockResponse(wttr_payload(temp_c=None, condition="Clear")), "valid current temperature"),
        (MockResponse(ValueError("not json")), "invalid weather data"),
    ],
)
def test_failed_or_malformed_wttr_responses_raise_controlled_errors(monkeypatch, response, message):
    skill, _ = load_skill(monkeypatch, response=response)

    with pytest.raises(skill.SkillError) as exc_info:
        skill.run({"city": "Toronto", "units": "metric"})

    assert message in str(exc_info.value)
    assert "Traceback" not in str(exc_info.value)


def test_json_stdin_stdout_returns_controlled_error_without_network_for_invalid_input(tmp_path):
    fake_requests_dir = tmp_path
    fake_requests_module = fake_requests_dir / "requests.py"
    fake_requests_module.write_text(
        "class RequestException(Exception):\n    pass\n\n"
        "def get(*args, **kwargs):\n    raise AssertionError('network should not be called')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps({"city": "Toronto", "units": "kelvin"}),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(fake_requests_dir)},
    )

    result = json.loads(completed.stdout)
    assert result == {"error": "Units must be either metric or imperial."}
    assert completed.stderr == ""
