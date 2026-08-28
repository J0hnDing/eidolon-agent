import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.integration_service import IntegrationError
from app.services.platform_service import (
    NOTION_DONE_CLEANUP_SERVICE_ID,
    NotionDoneCleanupService,
    PlatformServiceDispatcher,
    PlatformServiceError,
)


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

    assert result["status"] == "succeeded"
    assert result["scanned_count"] == 3
    assert result["deleted_ids"] == ["done-1", "done-2"]
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
    assert result["deleted_ids"] == ["deleted"]
    assert result["failures"] == [{"id": "failed", "error_type": "rate_limited"}]


def test_cleanup_normalizes_list_failure_without_deleting() -> None:
    fake = FakeIntegrations()
    fake.list_error = "connection_unavailable"

    result = service(fake).run()

    assert result["status"] == "failed"
    assert result["scanned_count"] == 0
    assert result["error_type"] == "connection_unavailable"


def test_platform_dispatcher_is_endpoint_only_and_not_in_function_catalog() -> None:
    engine = create_engine("sqlite:///:memory:")
    fake = FakeIntegrations()
    fake.pages = {None: {"todos": [], "has_more": False, "next_cursor": None}}
    with Session(engine) as db:
        dispatcher = PlatformServiceDispatcher(db, integrations=fake)  # type: ignore[arg-type]
        result = dispatcher.invoke(NOTION_DONE_CLEANUP_SERVICE_ID)
        with pytest.raises(PlatformServiceError, match="Unknown platform service"):
            dispatcher.invoke("unknown.service")

    assert result["status"] == "succeeded"
    seed_path = Path(__file__).resolve().parents[1] / "app" / "static" / "function_catalog_seed.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    assert NOTION_DONE_CLEANUP_SERVICE_ID not in {item["id"] for item in seed["functions"]}
