import asyncio

from mcp import types

from app.mcp_server import build_server
from app.services.mcp_function_service import McpFunctionError, McpInvocationResult


class FakeService:
    def __init__(self) -> None:
        self.tool = types.Tool(
            name="user_echo_abc12345",
            title="Echo",
            description="Echo one value.",
            inputSchema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            outputSchema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
        )

    def list_tools(self):
        return [self.tool]

    def invoke(self, name, arguments):
        if not isinstance(arguments.get("value"), str):
            raise McpFunctionError("invalid_input", "MCP tool input is invalid at value.")
        if arguments["value"] == "fail":
            raise McpFunctionError("bounded_failure", "Safe failure")
        return McpInvocationResult(output={"result": arguments["value"]}, summary="Completed safely.")


def test_server_lists_tools_returns_structured_content_and_sanitized_errors() -> None:
    server = build_server(FakeService())
    list_handler = server.request_handlers[types.ListToolsRequest]
    call_handler = server.request_handlers[types.CallToolRequest]

    listed = asyncio.run(list_handler(types.ListToolsRequest()))
    assert listed.root.tools[0].name == "user_echo_abc12345"

    called = asyncio.run(
        call_handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="user_echo_abc12345",
                    arguments={"value": "hello"},
                )
            )
        )
    )
    assert called.root.structuredContent == {"result": "hello"}
    assert called.root.content[0].text == "Completed safely."
    assert called.root.isError is False

    invalid = asyncio.run(
        call_handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="user_echo_abc12345",
                    arguments={"value": 7},
                )
            )
        )
    )
    assert invalid.root.isError is True
    assert invalid.root.content[0].text == "invalid_input: MCP tool input is invalid at value."

    failed = asyncio.run(
        call_handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="user_echo_abc12345",
                    arguments={"value": "fail"},
                )
            )
        )
    )
    assert failed.root.isError is True
    assert failed.root.content[0].text == "bounded_failure: Safe failure"
