from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from mcp import types
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.models import CodexMcpSettings, McpAuditRecord
from app.services.function_catalog_service import FunctionCatalogService

MAX_MCP_INPUT_BYTES = 256 * 1024
MAX_MCP_OUTPUT_BYTES = 1024 * 1024
MAX_MCP_ERROR_LENGTH = 512


def utc_now() -> datetime:
    return datetime.now(UTC)


class McpFunctionError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(" ".join(message.split())[:MAX_MCP_ERROR_LENGTH])
        self.error_type = error_type


@dataclass(frozen=True)
class McpToolSnapshot:
    name: str
    function_id: str
    category: str
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_only: bool
    destructive: bool
    open_world: bool
    contract_fingerprint: str
    requires_invocation_approval: bool

    def as_mcp_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            inputSchema=self.input_schema,
            outputSchema=self.output_schema,
            annotations=types.ToolAnnotations(
                title=self.title,
                readOnlyHint=self.read_only,
                destructiveHint=self.destructive,
                openWorldHint=self.open_world,
            ),
        )


@dataclass
class McpInvocationResult:
    output: dict[str, Any]
    summary: str


class McpFunctionService:
    """Snapshots catalog tools and routes bounded Codex MCP invocations."""

    def __init__(
        self,
        db: Session,
        *,
        project_root=None,
        agent_token: str | None = None,
        executor: InvocationExecutor | None = None,
    ) -> None:
        self.db = db
        self.project_root = project_root
        self.agent_token = agent_token
        self.executor = executor or InvocationExecutor(db, project_root=project_root)
        self.context_factory = InvocationContextFactory(db, project_root=project_root)
        self.agent_identity = None
        if agent_token is not None:
            from app.services.agent_policy_service import AgentPolicyService
            self.agent_identity = AgentPolicyService(db).authenticate(agent_token)
        entries = FunctionCatalogService(db, project_root=project_root).list_entries()
        if self.agent_identity is not None:
            from app.schemas.agents import AgentPlanRequest
            from app.services.agent_policy_service import AgentPolicyService
            entries = [entry for entry in entries if AgentPolicyService(db).decision(self.agent_identity[0], entry)[0]]
            if self.agent_identity[0] == "assistant":
                entries.append({"id": "plan_approval_request", "category": "agent_private", "title": "Request plan approval", "description": "Submit a concrete plan for user approval. Approval creates a new Act session; existing function approvals still apply. Backend saves proposal history.", "risk_level": "low", "availability": "available", "mcp_exposed": True, "input_schema": AgentPlanRequest.model_json_schema(), "output_schema": {"type": "object", "properties": {"status": {"type": "string"}, "proposal_id": {"type": "integer"}}, "required": ["status", "proposal_id"]}})
        self.excluded_ids = sorted(
            str(entry["id"])
            for entry in entries
            if entry.get("mcp_exposed") is False
        )
        snapshots = [
            self._snapshot(entry)
            for entry in entries
            if entry.get("availability") == "available" and entry.get("mcp_exposed") is True
        ]
        self._tools = {snapshot.name: snapshot for snapshot in snapshots}

    def list_tools(self) -> list[types.Tool]:
        if self.agent_token is not None:
            from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
            policy = AgentPolicyService(self.db)
            agent_id, _session_id = policy.authenticate(self.agent_token)
            result = []
            for name in sorted(self._tools):
                try:
                    policy.require_function(agent_id, self._tools[name].function_id)
                    result.append(self._tools[name].as_mcp_tool())
                except AgentPermissionError:
                    continue
            return result
        return [self._tools[name].as_mcp_tool() for name in sorted(self._tools)]

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def invoke(self, tool_name: str, arguments: dict[str, Any]) -> McpInvocationResult:
        if self.agent_token is not None:
            from app.services.agent_policy_service import AgentPermissionError

            try:
                context = self.context_factory.from_agent_credential(self.agent_token)
            except AgentPermissionError as exc:
                self._audit_agent_denial(tool_name, exc.error_type)
                raise McpFunctionError(exc.error_type, str(exc)) from None
        else:
            context = self.context_factory.direct_user(
                origin="codex_mcp",
                initiating_action="codex_mcp",
            )
        return self._invoke(tool_name, arguments, context)

    def _invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: InvocationContext,
    ) -> McpInvocationResult:
        snapshot = self._tools.get(tool_name)
        if snapshot is None:
            self._audit_agent_denial(tool_name, "unknown_tool")
            raise McpFunctionError("unknown_tool", "This Eidolon MCP tool is not in the process snapshot.")
        request_size = self._json_size(arguments)
        audit = McpAuditRecord(
            caller_type="codex_mcp",
            function_id=snapshot.function_id,
            category=snapshot.category,
            status="running",
            request_size=request_size,
            started_at=utc_now(),
        )
        if context.principal_kind == "agent":
            audit.caller_type = "agent"
            audit.agent_id = context.agent_id
            audit.agent_session_id = context.agent_session_id
            audit.agent_turn_id = context.agent_turn_id
        self.db.add(audit)
        self._commit_audit()
        try:
            if request_size > MAX_MCP_INPUT_BYTES:
                raise McpFunctionError("request_too_large", "MCP tool input exceeds the bounded request limit.")
            self._require_enabled()
            self._require_current_contract(snapshot)
            try:
                Draft202012Validator(snapshot.input_schema).validate(arguments)
            except ValidationError as exc:
                path = ".".join(str(item) for item in exc.absolute_path)
                location = f" at {path}" if path else ""
                raise McpFunctionError("invalid_input", f"MCP tool input is invalid{location}.") from None
            outcome = self.executor.execute(
                InvocationTargetRef(
                    category=snapshot.category,
                    target_id=snapshot.function_id,
                ),
                arguments,
                context,
            )
            if outcome.status not in {"succeeded", "partial", "pending_approval"}:
                raise McpFunctionError(
                    outcome.error_type or "function_failed",
                    outcome.error_message or f"Eidolon invocation {outcome.status}.",
                )
            if not isinstance(outcome.output, dict):
                raise McpFunctionError("internal_failure", "Eidolon invocation returned no object output.")
            output = outcome.output
            status = outcome.status
            audit.resource = outcome.audit_resource
            response_size = self._json_size(output)
            if response_size > MAX_MCP_OUTPUT_BYTES:
                raise McpFunctionError("response_too_large", "MCP tool output exceeds the bounded response limit.")
            audit.status = status
            audit.response_size = response_size
            audit.completed_at = utc_now()
            self._commit_audit()
            qualifier = (
                " with partial status"
                if status == "partial"
                else "; approval is pending"
                if status == "pending_approval"
                else ""
            )
            return McpInvocationResult(
                output=output,
                summary=f"Eidolon tool completed{qualifier} ({response_size} response bytes).",
            )
        except McpFunctionError as exc:
            self._fail_audit(audit, exc.error_type)
            raise
        except InvocationExecutionError as exc:
            self._fail_audit(audit, exc.error_type)
            raise McpFunctionError(exc.error_type, str(exc)) from None
        except Exception:
            self.db.rollback()
            self._fail_audit(audit, "internal_failure")
            raise McpFunctionError("internal_failure", "Eidolon MCP invocation failed safely.") from None

    def _audit_agent_denial(self, tool_name: str, error_type: str) -> None:
        if self.agent_identity is None:
            return
        # Resolve only backend catalog names; never persist arbitrary caller text.
        function_id = next((entry["id"] for entry in FunctionCatalogService(self.db).list_entries()
                            if self.tool_name(entry["category"], entry["id"]) == tool_name), "unknown_tool")
        self.db.add(McpAuditRecord(caller_type="agent", agent_id=self.agent_identity[0],
                                  agent_session_id=self.agent_identity[1], function_id=function_id,
                                  category="agent", status="failed", error_type=error_type,
                                  started_at=utc_now(), completed_at=utc_now()))
        self._commit_audit()

    def _require_enabled(self) -> None:
        if self.agent_token is not None:
            return
        self.db.expire_all()
        settings = self.db.get(CodexMcpSettings, 1)
        if settings is None or not settings.enabled:
            raise McpFunctionError("mcp_disabled", "Eidolon Codex tools are disabled in Settings.")

    def _require_current_contract(self, snapshot: McpToolSnapshot) -> None:
        if snapshot.category == "agent_private" and self.agent_identity is not None:
            return
        entries = FunctionCatalogService(self.db, project_root=self.project_root).list_entries()
        current = next(
            (entry for entry in entries if str(entry.get("id")) == snapshot.function_id),
            None,
        )
        if current is None or current.get("availability") != "available" or current.get("mcp_exposed") is not True:
            raise McpFunctionError("function_unavailable", "This function is no longer available.")
        if self._snapshot(current).contract_fingerprint != snapshot.contract_fingerprint:
            raise McpFunctionError(
                "stale_contract",
                "This tool contract changed after MCP startup. Restart the Codex session and try again.",
            )

    def _fail_audit(self, audit: McpAuditRecord, error_type: str) -> None:
        audit.status = "failed"
        audit.error_type = error_type[:64]
        audit.completed_at = utc_now()
        self._commit_audit()

    def _commit_audit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise McpFunctionError("audit_failure", "Eidolon MCP audit failed safely.") from None

    @classmethod
    def _snapshot(cls, entry: dict[str, Any]) -> McpToolSnapshot:
        function_id = str(entry["id"])
        category = str(entry["category"])
        input_schema = entry.get("input_schema")
        output_schema = entry.get("output_schema")
        if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
            raise McpFunctionError("invalid_contract", f"Function {function_id} has no object schemas.")
        identity = {
            "id": function_id,
            "category": category,
            "title": str(entry["title"]),
            "description": str(entry["description"]),
            "risk_level": str(entry["risk_level"]),
            "input_schema": input_schema,
            "output_schema": output_schema,
            "mcp_exposed": entry.get("mcp_exposed"),
            "mcp_read_only": entry.get("mcp_read_only"),
            "mcp_destructive": entry.get("mcp_destructive"),
            "mcp_open_world": entry.get("mcp_open_world"),
            "mcp_contract_fingerprint": entry.get("mcp_contract_fingerprint"),
            "requires_invocation_approval": entry.get("requires_invocation_approval", False),
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return McpToolSnapshot(
            name=cls.tool_name(category, function_id),
            function_id=function_id,
            category=category,
            title=str(entry["title"]),
            description=str(entry["description"]),
            input_schema=input_schema,
            output_schema=output_schema,
            read_only=entry.get("mcp_read_only") is True,
            destructive=entry.get("mcp_destructive") is True,
            open_world=entry.get("mcp_open_world") is True,
            contract_fingerprint=hashlib.sha256(encoded).hexdigest(),
            requires_invocation_approval=entry.get("requires_invocation_approval") is True,
        )

    @staticmethod
    def tool_name(category: str, function_id: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", function_id.casefold()).strip("_") or "function"
        digest = hashlib.sha256(function_id.encode("utf-8")).hexdigest()[:8]
        prefix = re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_") or "function"
        maximum_normalized_length = 128 - len(prefix) - len(digest) - 2
        normalized = normalized[:maximum_normalized_length].rstrip("_")
        return f"{prefix}_{normalized}_{digest}"

    @staticmethod
    def _json_size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
