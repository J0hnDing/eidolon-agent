import json
import subprocess
import sys
from pathlib import Path

import pytest


SKILL_PATH = Path(__file__).resolve().parents[1] / "skill.py"


def run_skill(payload):
    result = subprocess.run(
        [sys.executable, str(SKILL_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    assert result.returncode == 0
    return json.loads(result.stdout)


def test_adds_and_multiplies_with_precedence():
    assert run_skill({"expression": "1+2*3"}) == {"result": 7}


def test_supports_parentheses():
    assert run_skill({"expression": "(1+2)*3"}) == {"result": 9}


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('dir')",
        "open('secret.txt').read()",
        "[1, 2, 3]",
        "2 ** 999",
    ],
)
def test_rejects_unsafe_expressions(expression):
    output = run_skill({"expression": expression})

    assert "error" in output
