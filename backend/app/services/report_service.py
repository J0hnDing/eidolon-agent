from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ReportProvider(Protocol):
    def validate_identity(self) -> dict[str, str | None]: ...

    def validate_connection(self) -> dict[str, str | None]: ...

    def list(self, *, page_size: int, start_cursor: str | None) -> dict[str, Any]: ...

    def get(self, report_id: str, *, page_size: int, start_cursor: str | None) -> dict[str, Any]: ...

    def create(self, values: dict[str, Any]) -> dict[str, Any]: ...

    def delete(self, report_id: str) -> dict[str, Any]: ...


@dataclass
class ReportService:
    provider: ReportProvider

    def invoke(self, operation_id: str, input_json: dict[str, Any]) -> dict[str, Any]:
        if operation_id == "notion.report.list":
            return self.provider.list(
                page_size=int(input_json.get("page_size", 25)),
                start_cursor=input_json.get("start_cursor"),
            )
        if operation_id == "notion.report.get":
            return self.provider.get(
                str(input_json["id"]),
                page_size=int(input_json.get("page_size", 100)),
                start_cursor=input_json.get("start_cursor"),
            )
        if operation_id == "notion.report.create":
            return self.provider.create(input_json)
        if operation_id == "notion.report.delete":
            return self.provider.delete(str(input_json["id"]))
        raise ValueError("Report operation is unsupported")


class FakeReportProvider:
    """Deterministic credential-free provider for integration and service tests."""

    def __init__(
        self,
        *,
        bot_id: str = "fake-notion-bot",
        bot_name: str = "Fake Notion bot",
        workspace_name: str = "Fake workspace",
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reports: dict[str, dict[str, Any]] = {}
        self.blocks: dict[str, list[dict[str, Any]]] = {}
        self._counter = 0
        self.bot_id = bot_id
        self.bot_name = bot_name
        self.workspace_name = workspace_name

    def validate_connection(self) -> dict[str, str]:
        self.calls.append(("validate_connection", {}))
        return self._identity()

    def validate_identity(self) -> dict[str, str]:
        self.calls.append(("validate_identity", {}))
        return self._identity()

    def _identity(self) -> dict[str, str]:
        return {
            "bot_id": self.bot_id,
            "bot_name": self.bot_name,
            "workspace_name": self.workspace_name,
        }

    def list(self, *, page_size: int, start_cursor: str | None) -> dict[str, Any]:
        self.calls.append(("list", {"page_size": page_size, "start_cursor": start_cursor}))
        reports = sorted(self.reports.values(), key=lambda item: item["created_time"], reverse=True)
        offset = int(start_cursor or "0")
        selected = reports[offset : offset + page_size]
        next_offset = offset + len(selected)
        return {
            "reports": [dict(item) for item in selected],
            "has_more": next_offset < len(reports),
            "next_cursor": str(next_offset) if next_offset < len(reports) else None,
        }

    def get(self, report_id: str, *, page_size: int, start_cursor: str | None) -> dict[str, Any]:
        self.calls.append(
            ("get", {"id": report_id, "page_size": page_size, "start_cursor": start_cursor})
        )
        offset = int(start_cursor or "0")
        blocks = self.blocks[report_id]
        selected = blocks[offset : offset + page_size]
        next_offset = offset + len(selected)
        return {
            "report": dict(self.reports[report_id]),
            "blocks": [dict(item) for item in selected],
            "has_more": next_offset < len(blocks),
            "next_cursor": str(next_offset) if next_offset < len(blocks) else None,
        }

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", dict(values)))
        self._counter += 1
        report = {
            "id": f"fake-notion-report-{self._counter}",
            "name": values["name"],
            "created_time": f"2026-01-{self._counter:02d}T00:00:00Z",
            "select": values["select"],
        }
        self.reports[report["id"]] = report
        self.blocks[report["id"]] = [dict(item) for item in values["children"]]
        return dict(report)

    def delete(self, report_id: str) -> dict[str, Any]:
        self.calls.append(("delete", {"id": report_id}))
        self.reports.pop(report_id, None)
        self.blocks.pop(report_id, None)
        return {"id": report_id, "removed": True}
