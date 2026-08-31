from pathlib import Path

import pytest

from app.services.agent_workflow_service import AgentWorkflowService
from tests.fakes.codex import DeterministicCodexStub


@pytest.fixture(autouse=True)
def inject_test_codex_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.codex_service.default_codex_adapter",
        lambda: DeterministicCodexStub(),
    )
@pytest.fixture(autouse=True)
def isolate_default_agent_workflow_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = AgentWorkflowService.__post_init__

    def isolated(service: AgentWorkflowService) -> None:
        service.project_root = tmp_path
        original(service)

    monkeypatch.setattr(AgentWorkflowService, "__post_init__", isolated)
