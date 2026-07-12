from pathlib import Path

import pytest

from app.services.agent_workflow_service import AgentWorkflowService


@pytest.fixture(autouse=True)
def force_fake_codex_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_MODE", "fake")


@pytest.fixture(autouse=True)
def isolate_default_agent_workflow_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = AgentWorkflowService.__post_init__

    def isolated(service: AgentWorkflowService) -> None:
        service.project_root = tmp_path
        original(service)

    monkeypatch.setattr(AgentWorkflowService, "__post_init__", isolated)
