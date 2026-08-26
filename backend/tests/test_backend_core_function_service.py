import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.backend_core_function_service import (
    NOTION_DONE_CLEANUP_FUNCTION_ID,
    BackendCoreFunctionError,
    BackendCoreFunctionService,
    NotionDoneCleanupService,
)
from app.services.integration_service import IntegrationError


class FakeIntegrations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.pages: dict[str | None, dict] = {}
        self.delete_failures: dict[str, str] = {}
        self.list_error: str | None = None

    def invoke_direct(self, operation_id: str, input_json: dict) -> dict:
        self.calls.append((operation_id, dict(input_json)))
        if operation_id == "notion.todo.list":
            if self.list_error:
                raise IntegrationError(self.list_error, "safe list failure")
            return self.pages[input_json.get("start_cursor")]
        todo_id = input_json["id"]
        if todo_id in self.delete_failures:
            raise IntegrationError(self.delete_failures[todo_id], "safe delete failure")
        return {"id": todo_id, "removed": True}


def todo(todo_id: str, *, done: bool) -> dict:
    return {"id": todo_id, "done": done}


def service(fake: FakeIntegrations) -> NotionDoneCleanupService:
    return NotionDoneCleanupService(fake)  # type: ignore[arg-type]


def test_cleanup_paginates_and_deletes_only_done_todos() -> None:
    fake = FakeIntegrations()
    fake.pages = {
        None: {
            "todos": [todo("done-1", done=True), todo("open-1", done=False)],
            "has_more": True,
            "next_cursor": "cursor-2",
        },
        "cursor-2": {
            "todos": [todo("done-2", done=True)],
            "has_more": False,
            "next_cursor": None,
        },
    }

    result = service(fake).run()

    assert result == {
        "status": "succeeded",
        "scanned_count": 3,
        "matched_count": 2,
        "deleted_count": 2,
        "deleted_ids": ["done-1", "done-2"],
        "deleted_ids_truncated": False,
        "failures": [],
        "failures_truncated": False,
        "error_type": None,
    }
    assert [call for call in fake.calls if call[0] == "notion.todo.delete"] == [
        ("notion.todo.delete", {"id": "done-1"}),
        ("notion.todo.delete", {"id": "done-2"}),
    ]


def test_cleanup_continues_after_individual_delete_failure() -> None:
    fake = FakeIntegrations()
    fake.pages = {
        None: {
            "todos": [todo("failed", done=True), todo("deleted", done=True)],
            "has_more": False,
            "next_cursor": None,
        }
    }
    fake.delete_failures = {"failed": "rate_limited"}

    result = service(fake).run()

    assert result["status"] == "partial"
    assert result["matched_count"] == 2
    assert result["deleted_count"] == 1
    assert result["deleted_ids"] == ["deleted"]
    assert result["failures"] == [{"id": "failed", "error_type": "rate_limited"}]


def test_cleanup_normalizes_list_failure_without_deleting() -> None:
    fake = FakeIntegrations()
    fake.list_error = "connection_unavailable"

    result = service(fake).run()

    assert result["status"] == "failed"
    assert result["scanned_count"] == 0
    assert result["deleted_count"] == 0
    assert result["error_type"] == "connection_unavailable"


def test_backend_core_dispatch_is_scheduler_only_and_contract_shaped() -> None:
    engine = create_engine("sqlite:///:memory:")
    fake = FakeIntegrations()
    fake.pages = {None: {"todos": [], "has_more": False, "next_cursor": None}}
    with Session(engine) as db:
        dispatcher = BackendCoreFunctionService(db, integrations=fake)  # type: ignore[arg-type]
        with pytest.raises(BackendCoreFunctionError, match="scheduler-only"):
            dispatcher.invoke(NOTION_DONE_CLEANUP_FUNCTION_ID, {}, source="chat")
        with pytest.raises(BackendCoreFunctionError, match="does not accept input"):
            dispatcher.invoke(NOTION_DONE_CLEANUP_FUNCTION_ID, {"force": True}, source="scheduler")

        result = dispatcher.invoke(NOTION_DONE_CLEANUP_FUNCTION_ID, {}, source="scheduler")

    seed_path = Path(__file__).resolve().parents[1] / "app" / "static" / "function_catalog_seed.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    entry = next(item for item in seed["functions"] if item["id"] == NOTION_DONE_CLEANUP_FUNCTION_ID)
    Draft202012Validator(entry["output_schema"]).validate(result)
