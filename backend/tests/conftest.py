from pathlib import Path

import pytest

from app.services.agent_workflow_service import AgentWorkflowService
from tests.fakes.codex import DeterministicCodexStub


class _DeterministicDirectChatStub:
    def answer(self, prompt: str, message: str) -> str:
        return "Deterministic test chat response."


@pytest.fixture(autouse=True)
def inject_test_codex_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.codex_service.default_codex_adapter",
        lambda: DeterministicCodexStub(),
    )
    monkeypatch.setattr(
        "app.services.direct_chat_service.default_direct_chat_adapter",
        lambda: _DeterministicDirectChatStub(),
    )


@pytest.fixture(autouse=True)
def isolate_default_agent_workflow_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = AgentWorkflowService.__post_init__

    def isolated(service: AgentWorkflowService) -> None:
        service.project_root = tmp_path
        original(service)

    monkeypatch.setattr(AgentWorkflowService, "__post_init__", isolated)
