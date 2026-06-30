"""Simple calculator tool.

Reads a JSON object from stdin and writes a JSON object to stdout.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def calculate(operation: str, left: float, right: float) -> dict[str, Any]:
    """Return a calculator result object for the requested operation."""
    if operation == "add":
        return {"result": left + right, "error": None}
    if operation == "subtract":
        return {"result": left - right, "error": None}
    if operation == "multiply":
        return {"result": left * right, "error": None}
    if operation == "divide":
        if right == 0:
            return {"result": None, "error": "Division by zero is not allowed."}
        return {"result": left / right, "error": None}
    return {"result": None, "error": f"Unsupported operation: {operation}"}


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate input payload and calculate a result."""
    operation = payload.get("operation")
    left = payload.get("left", payload.get("a"))
    right = payload.get("right", payload.get("b"))

    if not isinstance(operation, str):
        return {"result": None, "error": "operation must be a string."}
    if not isinstance(left, int | float) or isinstance(left, bool):
        return {"result": None, "error": "left must be a number."}
    if not isinstance(right, int | float) or isinstance(right, bool):
        return {"result": None, "error": "right must be a number."}

    return calculate(operation, left, right)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"result": None, "error": f"Invalid JSON: {exc.msg}"}))
        return 1

    if not isinstance(payload, dict):
        print(json.dumps({"result": None, "error": "Input must be a JSON object."}))
        return 1

    output = handle(payload)
    print(json.dumps(output))
    return 0 if output.get("error") is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
