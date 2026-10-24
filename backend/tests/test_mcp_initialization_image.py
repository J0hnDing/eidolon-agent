import asyncio
import base64
from uuid import uuid4

import pytest
from mcp import types
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.execution.types import InvocationOutcome
from app.mcp_server import build_server
from app.models import ActSession, ActTurn, CodexMcpSettings, McpAuditRecord
from app.services import agent_policy_service
from app.services.agent_policy_service import AgentPolicyService
from app.services.mcp_function_service import McpFunctionError, McpFunctionService

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/qcAAAAASUVORK5CYII="
)


@pytest.fixture
def transport(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    catalog = []
    private = []
    monkeypatch.setattr("app.services.mcp_function_service.FunctionCatalogService.list_entries", lambda _: catalog)
    monkeypatch.setattr("app.services.mcp_function_service.assistant_private_tool_entries", lambda: private)
    monkeypatch.setattr("app.services.mcp_function_service.act_private_tool_entries", lambda: [])
    monkeypatch.setitem(agent_policy_service.PRIVATE_TOOL_AGENTS, "assistant.initialization.read", {"assistant"})
    with Session(engine) as db:
        db.add(CodexMcpSettings(id=1, enabled=True, config_fingerprint="owned"))
        db.commit()

        def create(output, *, agent="assistant", category="agent_private", function="assistant.initialization.read"):
            entry = dict(id=function, category=category, title="Read initialization", description="Read evidence",
                         risk_level="low", availability="available", mcp_exposed=True, mcp_read_only=True,
                         input_schema={"type": "object"}, output_schema={"type": "object"})
            catalog.clear()
            private.clear()
            (private if category == "agent_private" else catalog).append(entry)
            token = None
            if agent:
                session = ActSession(agent_id=agent, codex_thread_id=str(uuid4()))
                db.add(session)
                db.flush()
                if agent == "assistant":
                    db.add(ActTurn(session_id=session.id, user_message="Review Atlas initialization",
                                   status="running", assistant_context_json={"initialize_atlas": True}))
                db.commit()
                token = AgentPolicyService(db).issue(agent, session.id)

            class Executor:
                def execute(self, *_):
                    return InvocationOutcome(status="succeeded", output=output)

            service = McpFunctionService(db, agent_token=token, executor=Executor())
            return service, service.tool_name(category, function)

        yield db, create


def image_output(data=PNG, mime="image/png", **metadata):
    return {"attachment_id": "atlas-image-1", **metadata,
            "_initialization_image": {"data": base64.b64encode(data).decode(), "mime_type": mime}}


def test_private_initialization_returns_visual_content_and_metadata_without_image_audits(transport):
    db, create = transport
    output = image_output()
    service, name = create(output)
    server = build_server(service)
    called = asyncio.run(server.request_handlers[types.CallToolRequest](types.CallToolRequest(
        params=types.CallToolRequestParams(name=name, arguments={}),
    )))
    assert called.root.isError is False
    assert called.root.structuredContent == {"attachment_id": "atlas-image-1"}
    assert len(called.root.content) == 2
    image = called.root.content[1]
    assert isinstance(image, types.ImageContent)
    assert image.mimeType == "image/png"
    assert base64.b64decode(image.data) == PNG
    # Extraction must not mutate the source result or store image data in audits.
    assert "_initialization_image" in output
    audit = db.scalar(select(McpAuditRecord))
    assert audit.status == "succeeded"
    assert audit.response_size == len(PNG) + service._json_size(called.root.structuredContent)
    persisted = db.execute(McpAuditRecord.__table__.select()).mappings().one()
    assert image.data not in str(dict(persisted))


@pytest.mark.parametrize("mime", ["image/png", "image/jpeg", "image/gif", "image/webp"])
def test_private_image_channel_has_separate_size_bound_but_metadata_still_bounded(transport, monkeypatch, mime):
    _, create = transport
    monkeypatch.setattr("app.services.mcp_function_service.MAX_MCP_OUTPUT_BYTES", 100)
    service, name = create(image_output(b"a" * 200, mime))
    assert service.invoke(name, {}).image["mime_type"] == mime
    service, name = create(image_output(b"a" * 200, mime, oversized_metadata="x" * 100))
    with pytest.raises(McpFunctionError) as failure:
        service.invoke(name, {})
    assert failure.value.error_type == "response_too_large"


@pytest.mark.parametrize("marker", [
    None, {}, {"data": "", "mime_type": "image/png"},
    {"data": "not base64!", "mime_type": "image/png"},
    {"data": "é", "mime_type": "image/png"},
    {"data": "YQ==", "mime_type": []},
    {"data": 10, "mime_type": "image/png"},
    {"data": "YQ==", "mime_type": "image/svg+xml"},
    {"data": "YQ==", "mime_type": "image/png", "path": "secret"},
])
def test_malformed_initialization_images_fail_without_leaking_content(transport, marker):
    db, create = transport
    service, name = create({"_initialization_image": marker})
    with pytest.raises(McpFunctionError) as failure:
        service.invoke(name, {})
    assert failure.value.error_type == "invalid_image"
    assert db.scalar(select(McpAuditRecord)).error_type == "invalid_image"


@pytest.mark.parametrize("size", [10, 11, 12, 13])
def test_decoded_image_size_bound_including_base64_padding(transport, monkeypatch, size):
    _, create = transport
    monkeypatch.setattr("app.services.mcp_function_service.MAX_INITIALIZATION_IMAGE_BYTES", 10)
    service, name = create(image_output(b"x" * size))
    if size == 10:
        assert service.invoke(name, {}).image is not None
    else:
        with pytest.raises(McpFunctionError) as failure:
            service.invoke(name, {})
        assert failure.value.error_type == "response_too_large"


@pytest.mark.parametrize("agent,category,function", [
    (None, "agent_private", "assistant.initialization.read"),
    ("act", "agent_private", "assistant.initialization.read"),
    (None, "user", "assistant.initialization.read"),
    ("assistant", "user", "assistant.initialization.read"),
    ("assistant", "agent_private", "assistant.context.update"),
])
def test_image_exception_is_not_available_to_public_other_agents_or_other_tools(
    transport, monkeypatch, agent, category, function,
):
    _, create = transport
    monkeypatch.setattr("app.services.mcp_function_service.MAX_MCP_OUTPUT_BYTES", 100)
    service, name = create(image_output(b"x" * 200), agent=agent, category=category, function=function)
    with pytest.raises(McpFunctionError) as failure:
        service.invoke(name, {})
    assert failure.value.error_type in {"unknown_tool", "response_too_large"}


def test_initialization_without_image_preserves_ordinary_text_result(transport):
    _, create = transport
    service, name = create({"attachment_id": "text-1", "text": "Hello"})
    result = service.invoke(name, {})
    assert result.image is None
    assert result.output == {"attachment_id": "text-1", "text": "Hello"}
