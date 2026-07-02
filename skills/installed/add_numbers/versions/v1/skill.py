"""Add two numbers from JSON stdin and emit a JSON result."""

from __future__ import annotations

import json
import sys
from typing import Any


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def add_numbers(a: int | float, b: int | float) -> int | float:
    return a + b


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Input must be a JSON object.")

    expected_keys = {"a", "b"}
    actual_keys = set(payload)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        details = []
        if missing:
            details.append(f"missing required field(s): {', '.join(missing)}")
        if extra:
            details.append(f"unexpected field(s): {', '.join(extra)}")
        raise ValueError("; ".join(details))

    a = payload["a"]
    b = payload["b"]
    if not _is_number(a):
        raise ValueError("Field 'a' must be a number.")
    if not _is_number(b):
        raise ValueError("Field 'b' must be a number.")

    return {"result": add_numbers(a, b)}


def main() -> int:
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            raise ValueError("Expected JSON input on stdin.")
        payload = json.loads(raw_input)
        output = run(payload)
        print(json.dumps(output, separators=(",", ":")))
        return 0
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"Invalid JSON input: {exc.msg}"}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
