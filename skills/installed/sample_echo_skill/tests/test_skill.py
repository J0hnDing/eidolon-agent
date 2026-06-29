import json
import subprocess
import sys
from pathlib import Path

def test_sample_skill_outputs_json():
    skill_path = Path(__file__).resolve().parents[1] / 'skill.py'
    result = subprocess.run(
        [sys.executable, str(skill_path)],
        input=json.dumps({'message': 'Hello from the UI', 'label': 'Test run'}),
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output['title'] == 'Sample Echo Skill'
    assert output['summary'] == 'Test run: Hello from the UI'
    assert output['echoed_message'] == 'Hello from the UI'
    assert output['received_input'] == {'message': 'Hello from the UI', 'label': 'Test run'}
    assert output['warnings'] == []


def test_sample_skill_reports_invalid_json_as_json():
    skill_path = Path(__file__).resolve().parents[1] / 'skill.py'
    result = subprocess.run(
        [sys.executable, str(skill_path)],
        input="{not valid json}",
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output['summary'] == 'The input was not valid JSON.'
    assert output['warnings']
