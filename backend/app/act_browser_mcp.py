from __future__ import annotations

import asyncio
import os

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ActTurn
from app.services.act_browser_runtime_service import ActBrowserRuntimeError, browser_runtime_request
from app.services.agent_policy_service import AgentPolicyService


class ActBrowserMcpProxy:
    def __init__(self, endpoint: str, token: str, session_id: str, agent_token: str | None = None) -> None:
        self.endpoint, self.token, self.session_id = endpoint, token, session_id
        self.agent_token = agent_token

    def _authorize(self, *, require_turn: bool) -> None:
        if self.agent_token is None:
            return
        try:
            with SessionLocal() as db:
                agent_id, session_id = AgentPolicyService(db).authenticate(self.agent_token)
                if agent_id != "act" or str(session_id) != self.session_id:
                    raise ValueError("wrong session")
                if require_turn and db.scalar(select(ActTurn.id).where(
                    ActTurn.session_id == session_id, ActTurn.status == "running",
                    ActTurn.cancel_requested_at.is_(None),
                ).limit(1)) is None:
                    raise ValueError("inactive turn")
        except Exception:
            raise ActBrowserRuntimeError("Act browser authorization is unavailable") from None

    def list_tools(self) -> list[types.Tool]:
        self._authorize(require_turn=False)
        result = browser_runtime_request(self.endpoint, self.token, {
            "action": "mcp_list", "sessionId": int(self.session_id),
        })
        try:
            return [types.Tool.model_validate(tool) for tool in result["tools"]]
        except Exception:
            raise ActBrowserRuntimeError("Shared Act browser tool discovery is unavailable") from None

    def call_tool(self, name: str, arguments: dict) -> types.CallToolResult:
        try:
            self._authorize(require_turn=True)
            result = browser_runtime_request(self.endpoint, self.token, {
                "action": "mcp_call", "sessionId": int(self.session_id), "name": name, "arguments": arguments,
            }, timeout=180)
            return types.CallToolResult.model_validate(result)
        except Exception:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Shared Act browser is unavailable or not authorized")],
                isError=True,
            )


def build_server(proxy: ActBrowserMcpProxy) -> Server:
    server = Server("playwright", version="1.0.0")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return await asyncio.to_thread(proxy.list_tools)

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        return await asyncio.to_thread(proxy.call_tool, name, arguments)

    return server


async def _run_stdio() -> None:
    proxy = ActBrowserMcpProxy(
        os.environ.pop("EIDOLON_BROWSER_AUTH_ENDPOINT", ""),
        os.environ.pop("EIDOLON_BROWSER_AUTH_TOKEN", ""),
        os.environ.pop("EIDOLON_BROWSER_SESSION_ID", ""),
        os.environ.pop("EIDOLON_AGENT_TOKEN", None),
    )
    server = build_server(proxy)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
