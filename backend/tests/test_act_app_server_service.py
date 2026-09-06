from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.act_app_server_service import ActAppServerError, ActAppServerService
from app.services.act_workspace_service import ActWorkspace
from app.services.function_catalog_service import FunctionCatalogError, FunctionCatalogService


class FakeClient:
    def __init__(
        self,
        *,
        include_tools: bool = True,
        inherited_tools: bool = False,
        eidolon_ready: bool = True,
        paginate_inventory: bool = False,
        fail_thread_start: bool = False,
    ) -> None:
        self.include_tools = include_tools
        self.inherited_tools = inherited_tools
        self.eidolon_ready = eidolon_ready
        self.paginate_inventory = paginate_inventory
        self.fail_thread_start = fail_thread_start
        self.requests: list[tuple[str, object]] = []
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def request(self, method: str, params: object, *, timeout: float = 30):
        self.requests.append((method, params))
        if method == "config/mcpServer/reload":
            return {}
        if method == "plugin/installed":
            return {
                "marketplaces": [
                    {
                        "plugins": [
                            {"id": "remote-tools@example", "installed": True},
                            {"id": "available-only@example", "installed": False},
                        ]
                    }
                ]
            }
        if method == "mcpServerStatus/list":
            assert isinstance(params, dict)
            cursor = params.get("cursor")
            if self.paginate_inventory and cursor is None:
                return {
                    "data": [{"name": "legacy", "tools": {}, "resources": [], "resourceTemplates": []}],
                    "nextCursor": "page-2",
                }
            if self.paginate_inventory:
                assert cursor == "page-2"
            inventory = [
                {
                    "name": "legacy",
                    "tools": {},
                    "resources": [],
                    "resourceTemplates": [],
                },
                {
                    "name": "eidolon",
                    "serverInfo": (
                        {"name": "eidolon-agent"}
                        if self.eidolon_ready and params.get("threadId")
                        else None
                    ),
                    "tools": {"eidolon__tool": {}} if self.include_tools else {},
                    "resources": [],
                    "resourceTemplates": [],
                },
            ]
            if self.inherited_tools or self.started == 1:
                inventory.append(
                    {
                        "name": "unsafe-plugin",
                        "serverInfo": {"name": "unsafe-plugin"},
                        "tools": {"escape": {}},
                        "resources": [],
                        "resourceTemplates": [],
                    }
                )
            return {"data": inventory}
        if method == "thread/start":
            if self.fail_thread_start:
                token = params["config"]["mcp_servers.eidolon"]["env"][
                    "EIDOLON_AGENT_TOKEN"
                ]
                raise RuntimeError(f"invalid config env {token}")
            return {
                "thread": {"id": "thread-act"},
                "model": "gpt-act",
                "reasoningEffort": "high",
            }
        if method == "thread/resume":
            return {
                "thread": {"id": "thread-act"},
                "model": "gpt-act",
                "reasoningEffort": "high",
            }
        raise AssertionError(method)


def test_agent_thread_has_private_mcp_credential_and_named_permissions(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.models import ActSession
    workspace = ActWorkspace(root=tmp_path, memory=tmp_path / "memory", knowledge=tmp_path / "knowledge",
                             quercus=tmp_path / "knowledge" / "quercus", workspace=tmp_path / "workspace",
                             downloads=tmp_path / "workspace" / "downloads", instructions=tmp_path / "AGENTS.md")
    monkeypatch.setattr("app.services.act_app_server_service.ensure_act_workspace", lambda: workspace)
    monkeypatch.setattr("app.services.act_app_server_service.QuercusProcessingService", lambda db: SimpleNamespace(refresh_agent_instructions=lambda: None))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        session = ActSession(agent_id="observer", codex_thread_id="pending:test")
        db.add(session)
        db.commit()
        client = FakeClient()
        service = ActAppServerService(client, agent_id="observer")
        service.start_thread(db, model="gpt-act", reasoning_effort="high", session_id=session.id)
        params = next(params for method, params in client.requests if method == "thread/start")
        assert params["permissions"] == "eidolon_agent"
        assert "sandbox" not in params
        assert params["cwd"] == str(tmp_path.resolve())
        mcp = params["config"]["mcp_servers.eidolon"]
        assert mcp["required"] is True
        assert mcp["args"][-1] == "--agent"
        assert mcp["env"]["EIDOLON_AGENT_TOKEN"]
        assert "EIDOLON_AGENT_TOKEN" not in str(client.extra_args)
        assert ("plugin/installed", {}) in client.requests
        assert any("remote-tools@example" in item for item in client.extra_args)
        assert any("unsafe-plugin" in item for item in client.extra_args)
        assert client.started == 2
        assert ("mcpServerStatus/list", {"threadId": "thread-act"}) in client.requests


def test_agent_start_fails_closed_when_inherited_plugin_tools_remain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.models import ActSession

    workspace = ActWorkspace(
        root=tmp_path,
        memory=tmp_path / "memory",
        knowledge=tmp_path / "knowledge",
        quercus=tmp_path / "knowledge" / "quercus",
        workspace=tmp_path / "workspace",
        downloads=tmp_path / "workspace" / "downloads",
        instructions=tmp_path / "AGENTS.md",
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.QuercusProcessingService",
        lambda db: SimpleNamespace(refresh_agent_instructions=lambda: None),
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        session = ActSession(agent_id="observer", codex_thread_id="pending:test")
        db.add(session)
        db.commit()
        client = FakeClient(inherited_tools=True)
        service = ActAppServerService(client, agent_id="observer")

        with pytest.raises(ActAppServerError, match="inherited non-Eidolon"):
            service.start_thread(
                db,
                model="gpt-act",
                reasoning_effort="high",
                session_id=session.id,
            )

        assert all(method != "thread/start" for method, _ in client.requests)


def test_agent_start_requires_private_eidolon_server_after_thread_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.models import ActSession, AgentCredential

    workspace = ActWorkspace(
        root=tmp_path,
        memory=tmp_path / "memory",
        knowledge=tmp_path / "knowledge",
        quercus=tmp_path / "knowledge" / "quercus",
        workspace=tmp_path / "workspace",
        downloads=tmp_path / "workspace" / "downloads",
        instructions=tmp_path / "AGENTS.md",
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.QuercusProcessingService",
        lambda db: SimpleNamespace(refresh_agent_instructions=lambda: None),
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        session = ActSession(agent_id="observer", codex_thread_id="pending:test")
        db.add(session)
        db.commit()
        client = FakeClient(eidolon_ready=False)
        service = ActAppServerService(client, agent_id="observer")

        with pytest.raises(ActAppServerError, match="private Eidolon MCP server"):
            service.start_thread(
                db,
                model="gpt-act",
                reasoning_effort="high",
                session_id=session.id,
            )

        assert any(method == "thread/start" for method, _ in client.requests)
        assert db.scalar(select(AgentCredential.revoked)) is True


def test_agent_inventory_verification_reads_every_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.models import ActSession

    workspace = ActWorkspace(
        root=tmp_path,
        memory=tmp_path / "memory",
        knowledge=tmp_path / "knowledge",
        quercus=tmp_path / "knowledge" / "quercus",
        workspace=tmp_path / "workspace",
        downloads=tmp_path / "workspace" / "downloads",
        instructions=tmp_path / "AGENTS.md",
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.QuercusProcessingService",
        lambda db: SimpleNamespace(refresh_agent_instructions=lambda: None),
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        session = ActSession(agent_id="observer", codex_thread_id="pending:test")
        db.add(session)
        db.commit()
        client = FakeClient(paginate_inventory=True)

        ActAppServerService(client, agent_id="observer").start_thread(
            db,
            model="gpt-act",
            reasoning_effort="high",
            session_id=session.id,
        )

        assert any(
            method == "mcpServerStatus/list"
            and isinstance(params, dict)
            and params.get("cursor") == "page-2"
            and params.get("threadId") == "thread-act"
            for method, params in client.requests
        )


def test_agent_start_error_does_not_disclose_private_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.models import ActSession

    workspace = ActWorkspace(
        root=tmp_path,
        memory=tmp_path / "memory",
        knowledge=tmp_path / "knowledge",
        quercus=tmp_path / "knowledge" / "quercus",
        workspace=tmp_path / "workspace",
        downloads=tmp_path / "workspace" / "downloads",
        instructions=tmp_path / "AGENTS.md",
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.QuercusProcessingService",
        lambda db: SimpleNamespace(refresh_agent_instructions=lambda: None),
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        session = ActSession(agent_id="observer", codex_thread_id="pending:test")
        db.add(session)
        db.commit()
        client = FakeClient(fail_thread_start=True)

        with pytest.raises(ActAppServerError) as raised:
            ActAppServerService(client, agent_id="observer").start_thread(
                db,
                model="gpt-act",
                reasoning_effort="high",
                session_id=session.id,
            )

        params = next(params for method, params in client.requests if method == "thread/start")
        token = params["config"]["mcp_servers.eidolon"]["env"][
            "EIDOLON_AGENT_TOKEN"
        ]
        assert token not in str(raised.value)
        assert str(raised.value).endswith("RuntimeError")


def test_agent_process_config_restricts_paths_and_inherited_tools(monkeypatch, tmp_path):
    from app.services.act_app_server_service import managed_config
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[mcp_servers.untrusted]\ncommand="unsafe"\n[plugins.unsafe]\nenabled=true\n')
    config = managed_config("observer", tmp_path / "root")
    profile = config["permissions.eidolon_agent"]
    assert profile["network"]["enabled"] is False
    assert set(profile["filesystem"].values()) == {"read"}
    assert config["mcp_servers"]["untrusted"]["enabled"] is False
    assert config["plugins"]["unsafe"]["enabled"] is False
    assert config["web_search"] == "disabled"
    act = managed_config("act", tmp_path / "root")["permissions.eidolon_agent"]["filesystem"]
    assert act[str(tmp_path / "root" / "workspace")] == "write"
    assert act[str(tmp_path / "root" / "memory")] == "write"


def test_act_download_is_mcp_only_and_not_offered_to_project_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    service = FunctionCatalogService(None, project_root=tmp_path)  # type: ignore[arg-type]
    entry = {
        "id": "act.document.download",
        "category": "backend_core",
        "title": "Download",
        "description": "Act-only download",
        "risk_level": "medium",
        "availability": "available",
        "agent_selectable": False,
    }
    monkeypatch.setattr(service, "list_entries", lambda: [entry])

    assert service.available_index() == []
    with pytest.raises(FunctionCatalogError, match="reserved for Eidolon Act"):
        service.validate_available_ids(["act.document.download"])
