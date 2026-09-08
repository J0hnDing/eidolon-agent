from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

InvocationCategory = Literal["user", "integration", "backend_core", "agent_private"]


class InvocationExecutionError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(" ".join(str(message).split())[:2000])
        self.error_type = error_type


@dataclass(frozen=True)
class InvocationTargetRef:
    category: InvocationCategory
    target_id: str

    def __post_init__(self) -> None:
        if not self.target_id:
            raise ValueError("Invocation target id is required")


@dataclass(frozen=True)
class InvocationOutcome:
    status: str
    output: dict[str, Any] | list[Any] | None = None
    approval_id: int | None = None
    skill_run_id: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    audit_resource: str | None = None
