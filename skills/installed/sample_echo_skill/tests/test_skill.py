import json
import subprocess
import sys
from pathlib import Path

def test_sample_skill_outputs_json():
    skill_path = Path(__file__).resolve().parents[1] / 'skill.py'
    result = subprocess.run(
        [sys.executable, str(skill_path)],
        input=json.dumps({'hello': 'world'}),
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)['input'] == {'hello': 'world'}
