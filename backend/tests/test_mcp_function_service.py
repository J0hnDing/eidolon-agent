import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.execution.types import InvocationOutcome
from app.models import CodexMcpSettings, McpAuditRecord, Skill, SkillRun
from app.services.mcp_function_service import McpFunctionError, McpFunctionService


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def entry(
    function_id: str,
    category: str,
    *,
    available: bool = True,
    exposed: bool = True,
    read_only: bool = False,
    destructive: bool = False,
    open_world: bool = False,
) -> dict:
    return {
        "id": function_id,
        "category": category,
        "title": f"Title {function_id}",
        "description": f"Description {function_id}",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        "availability": "available" if available else "unavailable",
        "availability_reasons": [],
        "mcp_exposed": exposed,
        "mcp_read_only": read_only,
        "mcp_destructive": destructive,
        "mcp_open_world": open_world,
    }


def test_catalog_snapshot_preserves_contracts_annotations_and_future_entries(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        entry("backend.codex.call", "backend_core", exposed=False),
        entry("github.repository.get", "integration", read_only=True, open_world=True),
        entry("notion.todo.delete", "integration", destructive=True, open_world=True),
        entry("disabled.future", "integration", available=False),
        entry("future_user", "user"),
    ]
    monkeypatch.setattr(
        "app.services.mcp_function_service.FunctionCatalogService.list_entries",
        lambda _self: list(entries),
    )

    first = McpFunctionService(db)
    tools = {tool.name: tool for tool in first.list_tools()}

    assert len(tools) == 3
    assert first.excluded_ids == ["backend.codex.call"]
    github_name = McpFunctionService.tool_name("integration", "github.repository.get")
    assert github_name == "integration_github_repository_get_07cc15df"
    github = tools[github_name]
    assert github.title == "Title github.repository.get"
    assert github.description == "Description github.repository.get"
    assert github.inputSchema == entries[1]["input_schema"]
    assert github.outputSchema == entries[1]["output_schema"]
    assert github.annotations.readOnlyHint is True
    assert github.annotations.destructiveHint is False
    assert github.annotations.openWorldHint is True
    delete = tools[McpFunctionService.tool_name("integration", "notion.todo.delete")]
    assert delete.annotations.destructiveHint is True
    user = tools[McpFunctionService.tool_name("user", "future_user")]
    assert user.annotations.readOnlyHint is False

    entries.append(entry("future.integration.operation", "integration", read_only=True))
    assert first.tool_count == 3
    restarted = McpFunctionService(db)
    assert restarted.tool_count == 4


class FakeRunner:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.context = None

    def run(self, skill_id: int, skill_dir: Path, input_json: dict, context=None) -> SkillRun:
        self.context = context
        run = SkillRun(
            skill_id=skill_id,
            version_id=1,
            status="succeeded",
            input_json=input_json,
            output_json={"result": "ok"},
            invocation_source=context.invocation_source,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run


def test_user_invocation_uses_codex_mcp_history_and_sanitized_audit(
    db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_entry = entry("future_user", "user")
    monkeypatch.setattr(
        "app.services.mcp_function_service.FunctionCatalogService.list_entries",
        lambda _self: [catalog_entry],
    )
    db.add(CodexMcpSettings(id=1, enabled=True, config_fingerprint="owned"))
    target = Skill(
        name="future_user",
        description="Description future_user",
        runtime="function",
        status="installed",
        risk_level="low",
        manifest_path="unused",
        input_schema_json=catalog_entry["input_schema"],
        output_schema_json=catalog_entry["output_schema"],
        enabled=True,
    )
    db.add(target)
    db.commit()
    class Executor:
        def execute(self, target_ref, input_json, context):
            run = SkillRun(
                skill_id=target.id,
                status="succeeded",
                input_json=input_json,
                output_json={"result": "ok"},
                invocation_source=context.function_source(),
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            return InvocationOutcome(
                status="succeeded",
                output=run.output_json,
                skill_run_id=run.id,
            )

    service = McpFunctionService(db, project_root=tmp_path, executor=Executor())  # type: ignore[arg-type]
    sentinel = "MCP_ARGUMENT_SENTINEL_93ef"

    result = service.invoke(McpFunctionService.tool_name("user", "future_user"), {"value": sentinel})

    assert result.output == {"result": "ok"}
    run = db.scalar(select(SkillRun))
    assert run.invocation_source == "codex_mcp"
    audits = db.scalars(select(McpAuditRecord)).all()
    assert len(audits) == 1
    assert audits[0].status == "succeeded"
    assert audits[0].request_size == len(json.dumps({"value": sentinel}, separators=(",", ":")).encode())
    assert sentinel not in repr(audits[0])

    monkeypatch.setattr("app.services.mcp_function_service.MAX_MCP_OUTPUT_BYTES", 5)
    with pytest.raises(McpFunctionError) as oversized:
        service.invoke(McpFunctionService.tool_name("user", "future_user"), {"value": "safe"})
    assert oversized.value.error_type == "response_too_large"
    latest_audit = db.scalars(select(McpAuditRecord).order_by(McpAuditRecord.id.desc())).first()
    assert latest_audit.error_type == "response_too_large"


def test_revocation_reaches_multiple_snapshotted_processes_and_stale_contracts_fail(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [entry("future_user", "user")]
    monkeypatch.setattr(
        "app.services.mcp_function_service.FunctionCatalogService.list_entries",
        lambda _self: list(current),
    )
    settings = CodexMcpSettings(id=1, enabled=True, config_fingerprint="owned")
    db.add(settings)
    db.commit()
    first = McpFunctionService(db)
    second = McpFunctionService(db)
    tool_name = McpFunctionService.tool_name("user", "future_user")
    with pytest.raises(McpFunctionError) as invalid:
        first.invoke(tool_name, {"value": 7})
    assert invalid.value.error_type == "invalid_input"
    current[0] = {**current[0], "description": "Changed contract"}

    with pytest.raises(McpFunctionError) as stale:
        first.invoke(tool_name, {"value": "safe"})
    assert stale.value.error_type == "stale_contract"

    settings.enabled = False
    db.commit()
    with pytest.raises(McpFunctionError) as disabled_first:
        first.invoke(tool_name, {"value": "safe"})
    with pytest.raises(McpFunctionError) as disabled_second:
        second.invoke(tool_name, {"value": "safe"})
    assert disabled_first.value.error_type == "mcp_disabled"
    assert disabled_second.value.error_type == "mcp_disabled"
    assert all(
        audit.error_type in {"invalid_input", "stale_contract", "mcp_disabled"}
        for audit in db.scalars(select(McpAuditRecord))
    )
