from __future__ import annotations

import pytest

from app.services.act_app_server_service import (
    ACT_INSTRUCTIONS,
    ASSISTANT_INSTRUCTIONS_TEMPLATE,
    OBSERVER_INSTRUCTIONS,
    ActAppServerError,
    ActAppServerService,
    render_agent_instructions,
)
from app.services.act_runtime_capabilities import act_runtime_capability_catalog
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
        include_playwright: bool = False,
    ) -> None:
        self.include_tools = include_tools
        self.inherited_tools = inherited_tools
        self.eidolon_ready = eidolon_ready
        self.paginate_inventory = paginate_inventory
        self.fail_thread_start = fail_thread_start
        self.include_playwright = include_playwright
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
                            {"id": "codex-security@openai-curated-remote", "installed": True},
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
                {
                    "name": "codex-security",
                    "serverInfo": {"name": "Codex Security"},
                    "tools": {"security_scan": {}},
                    "resources": [],
                    "resourceTemplates": [],
                },
                {
                    "name": "openai-api-key-local-confirmation",
                    "serverInfo": {"name": "OpenAI Developers MCP"},
                    "tools": {
                        "confirm_openai_api_key_local_destination": {},
                    },
                    "resources": [],
                    "resourceTemplates": [],
                },
                {
                    "name": "openaiDeveloperDocs",
                    "serverInfo": {"name": "OpenAI Developer Docs"},
                    "tools": {"search_docs": {}, "fetch_page": {}},
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
            if self.include_playwright:
                inventory.append(
                    {
                        "name": "playwright",
                        "serverInfo": {"name": "Playwright MCP"},
                        "tools": {"browser_navigate": {}, "browser_click": {}},
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
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    workspace = ActWorkspace(root=tmp_path, memory=tmp_path / "memory", knowledge=tmp_path / "knowledge",
                             quercus=tmp_path / "knowledge" / "quercus", workspace=tmp_path / "workspace",
                             downloads=tmp_path / "workspace" / "downloads")
    monkeypatch.setattr("app.services.act_app_server_service.ensure_act_workspace", lambda: workspace)
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
        assert params["config"]["mcp_servers.legacy"]["enabled"] is False
        assert params["config"]["mcp_servers.unsafe-plugin"]["enabled"] is False
        assert params["config"]["mcp_servers.unsafe-plugin"]["command"] == "disabled"
        assert "mcp_servers.codex-security" not in params["config"]
        assert "mcp_servers.openai-api-key-local-confirmation" not in params["config"]
        assert "mcp_servers.openaiDeveloperDocs" not in params["config"]
        assert params["config"]["plugins.remote-tools@example"]["enabled"] is False
        assert "plugins.codex-security@openai-curated-remote" not in params["config"]
        assert params["config"]["apps._default.enabled"] is False
        assert params["config"]["features.apps"] is False
        assert params["config"]["features.remote_plugin"] is False
        assert "EIDOLON_AGENT_TOKEN" not in str(client.extra_args)
        assert ("plugin/installed", {}) in client.requests
        assert any("remote-tools@example" in item for item in client.extra_args)
        assert any("unsafe-plugin" in item for item in client.extra_args)
        assert client.started == 2
        assert ("mcpServerStatus/list", {"threadId": "thread-act"}) in client.requests
        assert params["developerInstructions"] == OBSERVER_INSTRUCTIONS


def test_managed_agents_receive_only_their_role_specific_instructions() -> None:
    assert render_agent_instructions("act", []) == ACT_INSTRUCTIONS
    assert render_agent_instructions("observer", []) == OBSERVER_INSTRUCTIONS
    assistant = render_agent_instructions("assistant", [])
    assert "[ACT_CAPABILITY_CATALOG]" not in assistant
    assert ASSISTANT_INSTRUCTIONS_TEMPLATE.count("[ACT_CAPABILITY_CATALOG]") == 1
    rendered = render_agent_instructions("assistant", [{"id": "example.read"}])
    assert '[{"id": "example.read"}]' in rendered
    with_functions = render_agent_instructions(
        "assistant", [], [{"id": "example.write", "input_schema": {"type": "object"}}],
    )
    assert '"id": "example.write"' in with_functions
    runtime_rendered = render_agent_instructions(
        "assistant",
        act_runtime_capability_catalog(),
    )
    assert '"id": "browser.playwright"' in runtime_rendered
    assert '"id": "browser.authenticate"' in runtime_rendered
    assert '"id": "telegram.send_file"' in runtime_rendered
    assert "## 1. Todo Doer" in assistant
    assert "## 2. Goal Doer" in assistant
    assert "## 3. Opportunity Scout" in assistant
    assert "[DETERMINISTIC_FUNCTION_CATALOG]" not in assistant
    assert ASSISTANT_INSTRUCTIONS_TEMPLATE.count("[DETERMINISTIC_FUNCTION_CATALOG]") == 1


def test_act_thread_receives_required_playwright_config(monkeypatch, tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db import Base
    from app.models import ActSession

    workspace = ActWorkspace(
        root=tmp_path / "act",
        memory=tmp_path / "act" / "memory",
        knowledge=tmp_path / "act" / "knowledge",
        quercus=tmp_path / "act" / "knowledge" / "quercus",
        workspace=tmp_path / "act" / "workspace",
        downloads=tmp_path / "act" / "workspace" / "downloads",
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.playwright]\ncommand="npx"\n'
        'args=["-y", "@playwright/mcp@latest"]\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        session = ActSession(agent_id="act", codex_thread_id="pending:test")
        db.add(session)
        db.commit()
        client = FakeClient(include_playwright=True)

        ActAppServerService(client, agent_id="act").start_thread(
            db,
            model="gpt-act",
            reasoning_effort="high",
            session_id=session.id,
        )

        params = next(params for method, params in client.requests if method == "thread/start")
        playwright = params["config"]["mcp_servers.playwright"]
        assert playwright["command"] == "npx"
        assert playwright["args"][:2] == ["-y", "@playwright/mcp@latest"]
        assert playwright["args"][-2] == "--init-page"
        assert playwright["args"][-1].endswith("browser_authentication_init_page.mjs")
        assert playwright["enabled"] is True
        assert playwright["default_tools_approval_mode"] == "approve"
        assert playwright["required"] is True
        bridge_env = playwright["env"]
        assert bridge_env["EIDOLON_BROWSER_AUTH_ENDPOINT"].startswith("\\\\.\\pipe\\eidolon-browser-auth-")
        assert bridge_env["EIDOLON_BROWSER_AUTH_TOKEN"]
        assert bridge_env["EIDOLON_BROWSER_AUTH_TOKEN"] not in params["developerInstructions"]
        assert "@oai/sky" in params["developerInstructions"]
        assert "Browser authenticate" in params["developerInstructions"]
        assert "Do not assume that graded coursework is prohibited" in params["developerInstructions"]


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
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
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
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
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
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
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
    )
    monkeypatch.setattr(
        "app.services.act_app_server_service.ensure_act_workspace", lambda: workspace
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
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.untrusted]\ncommand="unsafe"\n'
        '[mcp_servers.playwright]\ncommand="npx"\nargs=["-y", "@playwright/mcp@latest"]\n'
        '[plugins.unsafe]\nenabled=true\n'
    )
    root = tmp_path / "root"
    config = managed_config("observer", root)
    profile = config["permissions.eidolon_agent"]
    assert profile["network"]["enabled"] is False
    assert profile["filesystem"] == {
        ":root": "read",
        ":minimal": "read",
        str(root): "read",
    }
    assert config["mcp_servers"]["untrusted"]["enabled"] is False
    assert config["mcp_servers"]["playwright"]["enabled"] is False
    assert config["plugins"]["unsafe"]["enabled"] is False
    assert config["features.remote_plugin"] is False
    (tmp_path / "config.toml").write_text(
        '[plugins."codex-security@openai-curated-remote"]\nenabled=true\n'
        '[mcp_servers.codex-security]\ncommand="security"\n'
        '[mcp_servers.openai-api-key-local-confirmation]\ncommand="node"\n'
        '[mcp_servers.openaiDeveloperDocs]\nurl="https://developers.openai.com/mcp"\n'
    )
    security_config = managed_config("observer", root)
    assert security_config["plugins"]["codex-security@openai-curated-remote"]["enabled"] is True
    assert security_config["mcp_servers"]["codex-security"]["enabled"] is True
    assert security_config["mcp_servers"]["openai-api-key-local-confirmation"]["enabled"] is True
    assert security_config["mcp_servers"]["openaiDeveloperDocs"]["enabled"] is True
    assert security_config["mcp_servers"]["openaiDeveloperDocs"]["url"] == (
        "https://developers.openai.com/mcp"
    )
    assert config["web_search"] == "disabled"

    for agent_id in ("assistant", "act"):
        role_config = managed_config(agent_id, root)
        assert role_config["mcp_servers"]["openaiDeveloperDocs"]["enabled"] is True

    assistant = managed_config("assistant", root)["permissions.eidolon_agent"]
    assert assistant["filesystem"] == {
        ":root": "read",
        ":minimal": "read",
        str(root): "read",
    }
    assert assistant["network"]["enabled"] is False

    act = managed_config("act", root)["permissions.eidolon_agent"]
    assert act["filesystem"] == {
        ":root": "read",
        ":minimal": "read",
        str(root): "read",
        str(root / "workspace"): "write",
        str(root / "memory"): "write",
    }
    assert act["network"]["enabled"] is True
    assert str(root / "knowledge") not in act["filesystem"]

    act_config = managed_config("act", root)
    assert "untrusted" not in act_config["mcp_servers"]
    assert "playwright" not in act_config["mcp_servers"]


def test_managed_process_rejects_non_official_docs_mcp_endpoint(monkeypatch, tmp_path):
    import pytest

    from app.services.act_app_server_service import ActAppServerError, managed_config

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.openaiDeveloperDocs]\nurl="https://example.com/mcp"\n'
    )
    with pytest.raises(ActAppServerError, match="official OpenAI Developer Docs MCP"):
        managed_config("observer", tmp_path / "root")


def test_only_act_accepts_playwright_mcp_tools() -> None:
    service = ActAppServerService(FakeClient(), agent_id="act")
    service._mcp_inventory = lambda **_kwargs: [  # type: ignore[method-assign]
        {
            "name": "playwright",
            "serverInfo": {"name": "Playwright MCP"},
            "tools": {"browser_navigate": {}},
            "resources": [],
            "resourceTemplates": [],
        }
    ]
    service._verify_mcp_inventory(require_private_eidolon=False)

    observer = ActAppServerService(FakeClient(), agent_id="observer")
    observer._mcp_inventory = service._mcp_inventory  # type: ignore[method-assign]
    with pytest.raises(ActAppServerError, match="inherited non-Eidolon"):
        observer._verify_mcp_inventory(require_private_eidolon=False)


def test_act_requires_ready_playwright_tools() -> None:
    service = ActAppServerService(FakeClient(), agent_id="act")
    service._mcp_inventory = lambda **_kwargs: [  # type: ignore[method-assign]
        {
            "name": "playwright",
            "serverInfo": None,
            "tools": {},
            "resources": [],
            "resourceTemplates": [],
        }
    ]

    with pytest.raises(ActAppServerError, match="browser automation MCP server"):
        service._verify_mcp_inventory(require_private_eidolon=False)


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
