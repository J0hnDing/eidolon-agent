import subprocess
from pathlib import Path

import pytest

from app.services.codex_cli_service import CodexCliCompatibilityError, CodexCliService


def _version_runner(versions: dict[str, str]):
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        version = versions[str(command[0])]
        return subprocess.CompletedProcess(command, 0, stdout=f"codex-cli {version}\n", stderr="")

    return run


def test_automatic_resolution_selects_newest_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "path" / "codex.exe"
    bundled = tmp_path / "OpenAI" / "Codex" / "bin" / "codex.exe"
    stale.parent.mkdir()
    bundled.parent.mkdir(parents=True)
    stale.touch()
    bundled.touch()
    service = CodexCliService()
    monkeypatch.delenv("PERSONAL_AGENT_CODEX_COMMAND", raising=False)
    monkeypatch.setattr(service, "_automatic_paths", lambda: [str(stale), str(bundled)])
    monkeypatch.setattr(
        "app.services.codex_cli_service.subprocess.run",
        _version_runner({str(stale): "0.120.0", str(bundled): "0.140.0"}),
    )

    status = service.resolve()

    assert status.compatible is True
    assert status.resolved_path == str(bundled)
    assert status.version == "0.140.0"
    assert status.source == "codex_desktop"


def test_explicit_override_wins_even_when_another_cli_is_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-codex.exe"
    override.touch()
    service = CodexCliService()
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_COMMAND", str(override))
    monkeypatch.setattr(
        "app.services.codex_cli_service.subprocess.run",
        _version_runner({str(override): "0.130.0"}),
    )

    status = service.resolve()

    assert status.explicit_override is True
    assert status.resolved_path == str(override)
    assert status.source == "explicit_override"


def test_minimum_version_blocks_incompatible_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex.exe"
    executable.touch()
    service = CodexCliService()
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_COMMAND", str(executable))
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_MIN_VERSION", "0.140.0")
    monkeypatch.setattr(
        "app.services.codex_cli_service.subprocess.run",
        _version_runner({str(executable): "0.139.0"}),
    )

    status = service.resolve()

    assert status.available is True
    assert status.compatible is False
    assert "older than the required version 0.140.0" in (status.error or "")
    with pytest.raises(CodexCliCompatibilityError, match="older than the required version"):
        service.command()


def test_operation_can_add_a_future_capability_version_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex.exe"
    executable.touch()
    service = CodexCliService()
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_COMMAND", str(executable))
    monkeypatch.delenv("PERSONAL_AGENT_CODEX_MIN_VERSION", raising=False)
    monkeypatch.setattr(
        "app.services.codex_cli_service.subprocess.run",
        _version_runner({str(executable): "0.140.0"}),
    )

    with pytest.raises(CodexCliCompatibilityError, match="future model requires Codex CLI 0.150.0"):
        service.command(minimum_version="0.150.0", capability="future model")


def test_invalid_minimum_version_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    service = CodexCliService()
    monkeypatch.delenv("PERSONAL_AGENT_CODEX_COMMAND", raising=False)
    monkeypatch.setenv("PERSONAL_AGENT_CODEX_MIN_VERSION", "latest")

    status = service.resolve()

    assert status.compatible is False
    assert "semantic version" in (status.error or "")
