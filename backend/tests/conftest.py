import pytest


@pytest.fixture(autouse=True)
def force_fake_codex_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_MODE", "fake")
