from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.act_app_server_service import ActAppServerError, ActAppServerService
from app.services.act_workspace_service import ActWorkspace
from app.services.function_catalog_service import FunctionCatalogError, FunctionCatalogService


class FakeClient:
    def __init__(self, *, include_tools: bool = True) -> None:
        self.include_tools = include_tools
        self.requests: list[tuple[str, object]] = []
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        pass

    def request(self, method: str, params: object, *, timeout: float = 30):
        self.requests.append((method, params))
        if method == "config/mcpServer/reload":
            return {}
        if method == "mcpServerStatus/list":
            return {
                "data": [
                    {
                        "name": "eidolon",
                        "tools": {"eidolon__tool": {}} if self.include_tools else {},
                    }
                ]
            }
        if method == "thread/start":
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


def test_act_app_server_verifies_mcp_and_roots_thread_in_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    workspace = ActWorkspace(
        root=tmp_path / "act",
        memory=tmp_path / "act" / "memory",
        workspace=tmp_path / "act" / "workspace",
        downloads=tmp_path / "act" / "workspace" / "downloads",
        instructions=tmp_path / "act" / "AGENTS.md",
    )
    workspace.workspace.mkdir(parents=True)
    monkeypatch.setattr("app.services.act_app_server_service.ensure_act_workspace", lambda: workspace)
    monkeypatch.setattr(
        "app.services.act_app_server_service.CodexMcpSettingsService",
        lambda _db: SimpleNamespace(
            status=lambda: SimpleNamespace(enabled=True, config_matches=True)
        ),
    )
    client = FakeClient()
    service = ActAppServerService(client)  # type: ignore[arg-type]

    assert service.start_thread(None, model="gpt-act", reasoning_effort="high") == "thread-act"  # type: ignore[arg-type]

    start_params = next(params for method, params in client.requests if method == "thread/start")
    assert isinstance(start_params, dict)
    assert start_params["cwd"] == str(workspace.workspace.resolve())
    assert start_params["sandbox"] == "workspace-write"
    assert start_params["approvalPolicy"] == "never"

    # A just-started thread is already loaded in this App Server process. Resuming it
    # before its first turn can fail because Codex has not written a rollout yet.
    service.resume_thread(None, "thread-act", model="gpt-act", reasoning_effort="high")  # type: ignore[arg-type]
    assert not any(method == "thread/resume" for method, _params in client.requests)

    restarted_service = ActAppServerService(client)  # type: ignore[arg-type]
    restarted_service.resume_thread(None, "thread-act", model="gpt-act", reasoning_effort="high")  # type: ignore[arg-type]
    resume_params = next(params for method, params in reversed(client.requests) if method == "thread/resume")
    assert isinstance(resume_params, dict)
    assert resume_params["cwd"] == str(workspace.workspace.resolve())
    assert resume_params["sandbox"] == "workspace-write"
    assert resume_params["developerInstructions"]


def test_act_app_server_fails_closed_without_eidolon_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    workspace = ActWorkspace(
        root=tmp_path,
        memory=tmp_path / "memory",
        workspace=tmp_path / "workspace",
        downloads=tmp_path / "workspace" / "downloads",
        instructions=tmp_path / "AGENTS.md",
    )
    monkeypatch.setattr("app.services.act_app_server_service.ensure_act_workspace", lambda: workspace)
    monkeypatch.setattr(
        "app.services.act_app_server_service.CodexMcpSettingsService",
        lambda _db: SimpleNamespace(
            status=lambda: SimpleNamespace(enabled=True, config_matches=True)
        ),
    )
    service = ActAppServerService(FakeClient(include_tools=False))  # type: ignore[arg-type]

    with pytest.raises(ActAppServerError, match="without any Eidolon MCP tools"):
        service.ensure_ready(None)  # type: ignore[arg-type]


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
