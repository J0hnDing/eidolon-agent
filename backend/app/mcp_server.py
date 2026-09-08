from __future__ import annotations

import asyncio
import os
import sys

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from app.db import SessionLocal, create_db_and_tables
from app.services.mcp_function_service import McpFunctionError, McpFunctionService


def build_server(service: McpFunctionService) -> Server:
    server = Server(
        "eidolon-agent" if getattr(service, "agent_identity", None) is not None else "eidolon",
        version="1.0.0",
        instructions=(
            "Eidolon exposes the local user's currently available integration and installed-function catalog. "
            "Use each typed tool only for the user's explicit request. Tool contracts are snapshotted at startup; "
            "restart the Codex session after catalog changes."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return service.list_tools()

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        try:
            result = service.invoke(name, arguments)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=result.summary)],
                # MCP structuredContent is an object even when the native
                # integration result is an array; match the projected schema.
                structuredContent={"result": result.output} if isinstance(result.output, list) else result.output,
                isError=False,
            )
        except McpFunctionError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{exc.error_type}: {exc}")],
                isError=True,
            )

    return server


async def _run_stdio() -> None:
    create_db_and_tables()
    db = SessionLocal()
    try:
        agent_mode = "--agent" in sys.argv
        token = os.environ.pop("EIDOLON_AGENT_TOKEN", None) if agent_mode else None
        if agent_mode and not token:
            raise RuntimeError("Authenticated agent MCP requires a credential")
        server = build_server(McpFunctionService(db, agent_token=token))
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        db.close()


def main() -> None:
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
