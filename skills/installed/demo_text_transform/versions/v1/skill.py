from __future__ import annotations

import json
import sys
from typing import Any


def transform(payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    normalized = " ".join(text.split())
    return {
        "transformed_text": normalized.upper(),
        "character_count": len(normalized),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        print(json.dumps(transform(payload)))
        return 0
    except (json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
