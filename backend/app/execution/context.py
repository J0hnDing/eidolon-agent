from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

PrincipalKind = Literal["user", "agent", "skill", "web_app", "system"]
InvocationOrigin = Literal[
    "http",
    "codex_mcp",
    "agent_mcp",
    "skill_runtime",
    "web_app_runtime",
    "scheduler",
    "backend",
]


@dataclass(frozen=True)
class InvocationContext:
    """Immutable, backend-authenticated invocation attribution.

    The context deliberately contains identifiers only. Credentials and bearer
    capability tokens are consumed by InvocationContextFactory and never enter
    this object, approval metadata, or audit records.
    """

    principal_kind: PrincipalKind
    origin: InvocationOrigin
    agent_id: str | None = None
    agent_session_id: int | None = None
    agent_turn_id: int | None = None
    caller_skill_id: int | None = None
    caller_version_id: int | None = None
    caller_run_id: int | None = None
    caller_runtime: str | None = None
    web_app_instance_id: str | None = None
    source_schedule_id: int | None = None
    system_principal: str | None = None
    initiating_action: str | None = None

    def serialize(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def deserialize(cls, value: dict[str, Any]) -> InvocationContext:
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})

    def approval_caller_type(self) -> str:
        if self.principal_kind == "user":
            return "mcp" if self.origin == "codex_mcp" else "local_user"
        if self.principal_kind == "skill":
            return self.caller_runtime or "skill"
        return self.principal_kind

    def function_source(self) -> str:
        if self.principal_kind == "agent":
            return "codex_mcp"
        if self.principal_kind == "skill":
            return "skill"
        if self.principal_kind == "web_app":
            return "web_app"
        if self.principal_kind == "system":
            return "backend" if self.origin != "scheduler" else "schedule"
        return "codex_mcp" if self.origin == "codex_mcp" else "direct_user"

    def approval_source(self, category: str) -> str:
        if self.principal_kind == "agent":
            return f"agent:{self.agent_id}"
        if category == "integration":
            if self.principal_kind in {"skill", "web_app"}:
                return "integration_capability"
            if self.origin == "codex_mcp":
                return "mcp"
            return "direct_integration"
        return self.function_source()
