import asyncio
import json
import sys
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = PACKAGE_DIR.parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PACKAGE_DIR))

import web_runtime_capabilities  # noqa: E402
from app import TodoApp, app  # noqa: E402
from integration_test_adapter import DeterministicFakeIntegrationAdapter  # noqa: E402

TODO = {
    "id": "page-1",
    "title": "Buy groceries",
    "priority": "medium",
    "start_at": None,
    "due_at": "2026-08-30",
    "estimated_minutes": 20,
    "atlas_goal_id": None,
    "notes": "Milk and vegetables",
    "created_at": "2026-08-26T12:00:00Z",
}


def run_request(method, path, body=None):
    body_bytes = b"" if body is None else json.dumps(body).encode()
    messages = [{"type": "http.request", "body": body_bytes, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "method": method, "path": path, "raw_path": path.encode()}, receive, send))
    response_start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = next(message["body"] for message in sent if message["type"] == "http.response.body")
    content_type = dict(response_start["headers"])[b"content-type"].decode()
    parsed = json.loads(response_body) if content_type.startswith("application/json") else response_body.decode()
    return response_start["status"], parsed, sent


@pytest.fixture
def fake_integration(monkeypatch):
    adapter = DeterministicFakeIntegrationAdapter(
        {
            "notion.todo.list": {"todos": [TODO], "has_more": False, "next_cursor": None},
            "notion.todo.create": TODO,
            "notion.todo.update": {**TODO, "priority": None},
            "notion.todo.delete": {"id": "page-1", "removed": True},
        }
    )
    monkeypatch.setattr(web_runtime_capabilities, "call_integration", adapter.call)
    return adapter


def test_health_and_assets_are_available_without_provider_calls():
    assert run_request("GET", "/__personal_agent__/health")[0] == 200
    assert run_request("GET", "/")[0] == 200
    assert run_request("GET", "/static/app.js")[0] == 200


def test_app_resolves_the_runtime_cache_contract(monkeypatch):
    cache_path = PACKAGE_DIR / "cache"
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(cache_path))
    assert TodoApp().cache_dir == cache_path


def test_crud_routes_use_exact_selected_operations_and_schemas(fake_integration):
    status, body, _ = run_request("GET", "/api/todos?page_size=25")
    assert status == 200 and body["todos"][0]["id"] == "page-1"
    status, body, _ = run_request("POST", "/api/todos", {"title": "Buy groceries"})
    assert status == 200 and body["id"] == "page-1"
    status, body, _ = run_request("PATCH", "/api/todos/page-1", {"priority": None})
    assert status == 200 and body["priority"] is None
    status, body, _ = run_request("DELETE", "/api/todos/page-1")
    assert status == 200 and body == {"id": "page-1", "removed": True}
    assert [call["operation"] for call in fake_integration.calls] == [
        "notion.todo.list",
        "notion.todo.create",
        "notion.todo.update",
        "notion.todo.delete",
    ]
    assert fake_integration.calls[2]["input"] == {"id": "page-1", "priority": None}


def test_validation_rejects_unselected_fields_and_empty_updates(fake_integration):
    status, body, _ = run_request("POST", "/api/todos", {"title": "x", "credential": "nope"})
    assert status == 400 and body["error"] == "invalid_input"
    status, body, _ = run_request("PATCH", "/api/todos/page-1", {})
    assert status == 400 and body["error"] == "invalid_input"
    assert not fake_integration.calls


def test_provider_failure_is_normalized_without_leaking_details(monkeypatch):
    adapter = DeterministicFakeIntegrationAdapter(
        {}, failures={"notion.todo.list": ("provider_timeout", "secret provider diagnostics")}
    )
    monkeypatch.setattr(web_runtime_capabilities, "call_integration", adapter.call)
    status, body, _ = run_request("GET", "/api/todos")
    assert status == 502
    assert body == {"error": "provider_timeout", "message": "Notion took too long to respond. Try again shortly."}
    assert "secret" not in json.dumps(body).lower()


def test_browser_refreshes_after_mutations_and_explains_trash():
    javascript = (PACKAGE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    assert javascript.count("await loadTodos();") >= 1
    assert "notion.todo" not in javascript
    assert "moved to Notion trash" in javascript
    assert "window.confirm" in javascript
