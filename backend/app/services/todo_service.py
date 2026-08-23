from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class TodoProvider(Protocol):
    def validate_connection(self) -> dict[str, str | None]: ...

    def list(self, *, page_size: int, start_cursor: str | None) -> dict[str, Any]: ...

    def create(self, values: dict[str, Any]) -> dict[str, Any]: ...

    def update(self, todo_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...

    def delete(self, todo_id: str) -> dict[str, Any]: ...


@dataclass
class TodoService:
    provider: TodoProvider

    def invoke(self, operation_id: str, input_json: dict[str, Any]) -> dict[str, Any]:
        if operation_id == "notion.todo.list":
            return self.provider.list(
                page_size=int(input_json.get("page_size", 25)),
                start_cursor=input_json.get("start_cursor"),
            )
        if operation_id == "notion.todo.create":
            return self.provider.create(input_json)
        if operation_id == "notion.todo.update":
            return self.provider.update(
                str(input_json["id"]),
                {key: value for key, value in input_json.items() if key != "id"},
            )
        if operation_id == "notion.todo.delete":
            return self.provider.delete(str(input_json["id"]))
        raise ValueError("Todo operation is unsupported")


class FakeTodoProvider:
    """Deterministic credential-free provider for service and generated-skill tests."""

    def __init__(
        self,
        *,
        bot_id: str = "fake-notion-bot",
        bot_name: str = "Fake Notion bot",
        workspace_name: str = "Fake workspace",
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.todos: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self.bot_id = bot_id
        self.bot_name = bot_name
        self.workspace_name = workspace_name

    def validate_connection(self) -> dict[str, str]:
        self.calls.append(("validate_connection", {}))
        return {
            "bot_id": self.bot_id,
            "bot_name": self.bot_name,
            "workspace_name": self.workspace_name,
        }

    def list(self, *, page_size: int, start_cursor: str | None) -> dict[str, Any]:
        self.calls.append(("list", {"page_size": page_size, "start_cursor": start_cursor}))
        todos = sorted(self.todos.values(), key=lambda item: item["created_at"], reverse=True)
        offset = int(start_cursor or "0")
        selected = todos[offset : offset + page_size]
        next_offset = offset + len(selected)
        return {
            "todos": [dict(item) for item in selected],
            "has_more": next_offset < len(todos),
            "next_cursor": str(next_offset) if next_offset < len(todos) else None,
        }

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", dict(values)))
        self._counter += 1
        todo = {
            "id": f"fake-notion-page-{self._counter}",
            "title": values["title"],
            "priority": values.get("priority"),
            "start_at": values.get("start_at"),
            "due_at": values.get("due_at"),
            "estimated_minutes": values.get("estimated_minutes"),
            "atlas_goal_id": values.get("atlas_goal_id"),
            "notes": values.get("notes"),
            "created_at": f"2026-01-{self._counter:02d}T00:00:00Z",
        }
        self.todos[todo["id"]] = todo
        return dict(todo)

    def update(self, todo_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("update", {"id": todo_id, **changes}))
        todo = self.todos[todo_id]
        todo.update(changes)
        return dict(todo)

    def delete(self, todo_id: str) -> dict[str, Any]:
        self.calls.append(("delete", {"id": todo_id}))
        self.todos.pop(todo_id, None)
        return {"id": todo_id, "removed": True}
