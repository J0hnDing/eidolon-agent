import json
import subprocess
import sys

from skill import calculate, handle


def test_add_returns_sum():
    assert calculate("add", 2, 3) == {"result": 5, "error": None}


def test_subtract_returns_difference():
    assert calculate("subtract", 7, 4) == {"result": 3, "error": None}


def test_multiply_returns_product():
    assert calculate("multiply", 3, 5) == {"result": 15, "error": None}


def test_divide_returns_quotient():
    assert calculate("divide", 8, 2) == {"result": 4, "error": None}


def test_divide_by_zero_returns_error():
    assert calculate("divide", 8, 0) == {
        "result": None,
        "error": "Division by zero is not allowed.",
    }


def test_unknown_operation_returns_error():
    assert calculate("power", 2, 3) == {
        "result": None,
        "error": "Unsupported operation: power",
    }


def test_handle_accepts_a_b_aliases():
    assert handle({"operation": "add", "a": 4, "b": 6}) == {
        "result": 10,
        "error": None,
    }


def test_cli_reads_json_from_stdin_and_writes_json_to_stdout():
    completed = subprocess.run(
        [sys.executable, "skill.py"],
        input=json.dumps({"operation": "subtract", "left": 9, "right": 4}),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"result": 5, "error": None}
    assert completed.stderr == ""
