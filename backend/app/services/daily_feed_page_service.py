from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DailyFeedPageProvider(Protocol):
    def validate_identity(self) -> dict[str, str | None]: ...

    def validate_connection(self) -> dict[str, str | None]: ...

    def write(self, markdown: str) -> dict[str, object]: ...


@dataclass
class DailyFeedPageService:
    provider: DailyFeedPageProvider

    def invoke(self, operation_id: str, input_json: dict[str, object]) -> dict[str, object]:
        if operation_id == "notion.daily_feed.write":
            return self.provider.write(str(input_json["markdown"]))
        raise ValueError("Daily Feed page operation is unsupported")


class FakeDailyFeedPageProvider:
    """Deterministic credential-free provider for integration tests."""

    def __init__(
        self,
        *,
        bot_id: str = "fake-notion-bot",
        bot_name: str = "Fake Notion bot",
        workspace_name: str = "Fake workspace",
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.markdown = ""
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

    def write(self, markdown: str) -> dict[str, object]:
        self.calls.append(("write", {"markdown": markdown}))
        self.markdown = markdown
        return {"updated": True, "characters": len(markdown)}
