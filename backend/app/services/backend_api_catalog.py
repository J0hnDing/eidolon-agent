import json
from functools import lru_cache
from pathlib import Path
from typing import Any

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
BACKEND_API_INDEX_PATH = STATIC_DIR / "backend_api_index.json"
BACKEND_API_CONTEXT_PATH = STATIC_DIR / "backend_api_context.json"


def backend_api_index_file() -> Path:
    return BACKEND_API_INDEX_PATH


def backend_api_context_file() -> Path:
    return BACKEND_API_CONTEXT_PATH


@lru_cache(maxsize=1)
def _backend_api_index_payload() -> dict[str, Any]:
    return _read_static_json(BACKEND_API_INDEX_PATH)


@lru_cache(maxsize=1)
def _backend_api_context_payload() -> dict[str, Any]:
    return _read_static_json(BACKEND_API_CONTEXT_PATH)


def _read_static_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Static backend API file must contain a JSON object: {path}")
    return payload


def backend_api_index() -> list[dict[str, Any]]:
    entries = _backend_api_index_payload().get("backend_apis", [])
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def backend_api_context(api_ids: object) -> list[dict[str, Any]]:
    if not isinstance(api_ids, list):
        return []
    requested: list[int] = []
    for raw_id in api_ids:
        try:
            api_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if api_id not in requested:
            requested.append(api_id)
    by_id = {
        int(entry["id"]): entry
        for entry in _backend_api_context_payload().get("backend_api_contexts", [])
        if isinstance(entry, dict) and "id" in entry
    }
    return [dict(by_id[api_id]) for api_id in requested if api_id in by_id]


def valid_backend_api_ids() -> set[int]:
    return {int(entry["id"]) for entry in backend_api_index() if "id" in entry}
