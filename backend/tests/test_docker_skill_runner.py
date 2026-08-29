import json
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill
from app.services.docker_image_manager import DockerImageBuildError, DockerImageStatus
from app.services.skill_runner import DockerSkillRunner, RunnerConfig, get_runner_status, is_docker_available


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def create_skill_record(db: Session, skill_dir: Path, status: str = "installed", enabled: bool = True) -> Skill:
    skill = Skill(
        name=f"docker_skill_{len(list(skill_dir.parent.iterdir()))}",
        description="Docker runner test skill",
        status=status,
        risk_level="low",
        manifest_path=str(skill_dir / "manifest.json"),
        installed_path=str(skill_dir),
        enabled=enabled,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def write_skill(
    skill_dir: Path,
    manifest_overrides: dict[str, Any] | None = None,
    test_source: str = "def test_skill_passes():\n    assert True\n",
) -> None:
    skill_dir.mkdir()
    (skill_dir / "tests").mkdir()
    manifest = {
        "name": "docker_demo_skill",
        "description": "A trusted local demo skill.",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "risk_level": "low",
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "codex",
        "enabled": False,
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)

    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "print(json.dumps({'ok': True, 'input': payload}))\n",
        encoding="utf-8",
    )
    (skill_dir / "tests" / "test_skill.py").write_text(test_source, encoding="utf-8")


def config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        mode="docker",
        docker_image="test-skill-runner:latest",
        timeout_seconds=3,
        memory_limit="128m",
        cpu_limit="0.5",
        runtime_root=tmp_path / "runtime",
    )


def test_default_runner_timeout_supports_bounded_network_workflows() -> None:
    assert RunnerConfig().timeout_seconds == 120


def test_private_function_capability_command_uses_internal_network_and_relay_url(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill_dir = tmp_path / "private_capability_skill"
    skill_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    entrypoint = skill_dir / "skill.py"
    entrypoint.write_text("", encoding="utf-8")
    runner = make_runner(db_session, tmp_path, lambda *args, **kwargs: completed())

    command = runner.build_entrypoint_command(
        skill_dir,
        cache_dir,
        entrypoint,
        skill_id=4,
        capability_token="ephemeral-secret",
        network_mode_override="private-function-network",
        backend_url_override="http://trusted-function-relay:8000",
    )

    assert command[command.index("--network") + 1] == "private-function-network"
    assert "api.github.com:127.0.0.1" in command
    assert "github.com:127.0.0.1" in command
    assert "api.notion.com:127.0.0.1" in command
    assert "www.googleapis.com:127.0.0.1" in command
    assert "oauth2.googleapis.com:127.0.0.1" in command
    assert "PERSONAL_AGENT_BACKEND_URL=http://trusted-function-relay:8000" in command
    assert "PERSONAL_AGENT_FUNCTION_CAPABILITY=ephemeral-secret" in command


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr)


def make_runner(
    db: Session,
    tmp_path: Path,
    docker_runner: Callable[..., subprocess.CompletedProcess[str]],
    available: bool = True,
    image_manager: Any | None = None,
) -> DockerSkillRunner:
    return DockerSkillRunner(
        db,
        config=config(tmp_path),
        docker_runner=docker_runner,
        docker_available_checker=lambda: available,
        image_manager=image_manager or FakeBuiltImageManager(),
    )


class FakeBuiltImageManager:
    def ensure_image(self) -> DockerImageStatus:
        return DockerImageStatus(
            image="test-skill-runner:latest",
            status="built",
            dockerfile_hash="hash",
            last_successful_hash="hash",
            detail="Image already built.",
        )


class FakeFailingImageManager:
    def ensure_image(self) -> DockerImageStatus:
        raise DockerImageBuildError("Trusted Docker runner image build failed.\nmissing package")


def test_docker_runner_builds_restricted_commands(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "command_skill"
    write_skill(skill_dir)
    skill = create_skill_record(db_session, skill_dir)
    commands: list[list[str]] = []

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "/skill/tests" in command:
            return completed(stdout="tests passed")
        return completed(stdout='{"ok": true}')

    run = make_runner(db_session, tmp_path, fake_runner).run(skill.id, skill_dir, {"topic": "sandbox"})

    assert run.status == "succeeded"
    assert len(commands) == 2
    entrypoint_command = commands[1]
    assert entrypoint_command[:3] == ["docker", "run", "--rm"]
    assert "--network" in entrypoint_command
    assert entrypoint_command[entrypoint_command.index("--network") + 1] == "none"
    assert "--memory" in entrypoint_command
    assert entrypoint_command[entrypoint_command.index("--memory") + 1] == "128m"
    assert "--cpus" in entrypoint_command
    assert entrypoint_command[entrypoint_command.index("--cpus") + 1] == "0.5"
    assert f"{skill_dir.resolve()}:/skill:ro" in entrypoint_command
    assert f"{(tmp_path / 'runtime' / f'skill_{skill.id}' / 'cache').resolve()}:/skill/cache:rw" in entrypoint_command
    assert "test-skill-runner:latest" in entrypoint_command
    assert (skill_dir / "cache").is_dir()


def test_docker_runner_allows_explicit_network_permissions_with_bridge_network(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "network_skill"
    write_skill(
        skill_dir,
        {
            "risk_level": "medium",
            "permissions": {
                "network": ["example.com"],
                "filesystem_read": [],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            }
        },
    )
    skill = create_skill_record(db_session, skill_dir)
    commands: list[list[str]] = []

    run = make_runner(db_session, tmp_path, lambda command, **_: commands.append(command) or completed()).run(
        skill.id, skill_dir, {}
    )

    assert run.status == "failed"
    assert len(commands) == 2
    assert commands[0][commands[0].index("--network") + 1] == "bridge"
    assert commands[1][commands[1].index("--network") + 1] == "bridge"
    assert "api.github.com:127.0.0.1" in commands[1]


def test_docker_runner_blocks_filesystem_read(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "read_skill"
    write_skill(
        skill_dir,
        {
            "risk_level": "high",
            "permissions": {
                "network": [],
                "filesystem_read": ["./data"],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            }
        },
    )
    skill = create_skill_record(db_session, skill_dir)

    run = make_runner(db_session, tmp_path, lambda command, **_: completed()).run(skill.id, skill_dir, {})

    assert run.status == "blocked"
    assert run.error_message == "filesystem read permissions are limited to the skill's own ./cache directory"


def test_docker_runner_blocks_shell_true(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "shell_skill"
    write_skill(
        skill_dir,
        {
            "risk_level": "high",
            "permissions": {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": True,
            }
        },
    )
    skill = create_skill_record(db_session, skill_dir)

    run = make_runner(db_session, tmp_path, lambda command, **_: completed()).run(skill.id, skill_dir, {})

    assert run.status == "blocked"
    assert run.error_message == "shell permissions are not supported by the current runner"


def test_docker_runner_allows_cache_write_permission(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "cache_skill"
    write_skill(skill_dir)
    skill = create_skill_record(db_session, skill_dir)

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "/skill/tests" in command:
            return completed(stdout="tests passed")
        return completed(stdout='{"ok": true, "cache": "allowed"}')

    run = make_runner(db_session, tmp_path, fake_runner).run(skill.id, skill_dir, {})

    assert run.status == "succeeded"
    assert run.output_json == {"ok": True, "cache": "allowed"}


def test_docker_runner_blocks_unsafe_cache_mountpoint(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "bad_cache_skill"
    write_skill(skill_dir)
    (skill_dir / "cache").write_text("not a directory", encoding="utf-8")
    skill = create_skill_record(db_session, skill_dir)

    run = make_runner(db_session, tmp_path, lambda command, **_: completed()).run(skill.id, skill_dir, {})

    assert run.status == "blocked"
    assert run.error_message == "Skill cache mountpoint must be a regular directory"


def test_docker_runner_stores_failed_invalid_json_run(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "invalid_json_skill"
    write_skill(skill_dir)
    skill = create_skill_record(db_session, skill_dir)

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "/skill/tests" in command:
            return completed(stdout="tests passed")
        return completed(stdout="not json")

    run = make_runner(db_session, tmp_path, fake_runner).run(skill.id, skill_dir, {})

    assert run.status == "failed"
    assert "not valid JSON" in (run.error_message or "")
    assert db_session.get(type(run), run.id) is not None


def test_docker_runner_handles_timeout(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "timeout_skill"
    write_skill(skill_dir)
    skill = create_skill_record(db_session, skill_dir)

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "/skill/tests" in command:
            return completed(stdout="tests passed")
        raise subprocess.TimeoutExpired(cmd=command, timeout=3, output="partial", stderr="late")

    run = make_runner(db_session, tmp_path, fake_runner).run(skill.id, skill_dir, {})

    assert run.status == "failed"
    assert run.stdout == "partial"
    assert run.stderr == "late"
    assert run.error_message == "Skill timed out after 3 seconds"


def test_docker_runner_blocks_when_docker_unavailable(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "unavailable_skill"
    write_skill(skill_dir)
    skill = create_skill_record(db_session, skill_dir)
    commands: list[list[str]] = []

    run = make_runner(
        db_session,
        tmp_path,
        lambda command, **_: commands.append(command) or completed(),
        available=False,
    ).run(skill.id, skill_dir, {})

    assert run.status == "blocked"
    assert "Docker sandbox runner is selected, but Docker is unavailable" in (run.error_message or "")
    assert commands == []


def test_docker_runner_blocks_when_image_build_fails(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "build_fail_skill"
    write_skill(skill_dir)
    skill = create_skill_record(db_session, skill_dir)
    commands: list[list[str]] = []

    run = make_runner(
        db_session,
        tmp_path,
        lambda command, **_: commands.append(command) or completed(),
        image_manager=FakeFailingImageManager(),
    ).run(skill.id, skill_dir, {})

    assert run.status == "blocked"
    assert "Trusted Docker runner image build failed" in (run.error_message or "")
    assert commands == []


def test_runner_status_requires_explicit_local_fallback() -> None:
    auto_status = get_runner_status(config=RunnerConfig(mode="auto"), docker_available_checker=lambda: False)
    local_status = get_runner_status(config=RunnerConfig(mode="local"), docker_available_checker=lambda: False)

    assert auto_status.selected_mode == "docker"
    assert auto_status.available is False
    assert local_status.selected_mode == "local"
    assert local_status.available is True


def test_docker_availability_checks_daemon_connectivity(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import skill_runner

    commands: list[list[str]] = []
    monkeypatch.setattr(skill_runner.shutil, "which", lambda _name: "C:/Docker/docker.exe")

    def unavailable_daemon(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return completed(stderr="daemon unavailable", returncode=1)

    monkeypatch.setattr(skill_runner.subprocess, "run", unavailable_daemon)

    assert is_docker_available() is False
    assert commands == [["docker", "info", "--format", "{{json .ServerVersion}}"]]


@pytest.mark.parametrize(
    ("image_status", "expected_available", "expected_ready"),
    [("built", True, True), ("failed", False, False), ("unverified", False, False)],
)
def test_runner_status_requires_current_trusted_image(
    image_status: str,
    expected_available: bool,
    expected_ready: bool,
) -> None:
    class FakeImageManager:
        def get_status(self) -> DockerImageStatus:
            return DockerImageStatus(
                image="personal-agent-skill-runner:test",
                status=image_status,
                dockerfile_hash="current",
                last_successful_hash="current" if image_status == "built" else None,
                detail="Image status for test.",
                last_build_status="failed" if image_status != "built" else "built",
                last_build_at="2026-08-28T00:00:00Z",
            )

    status = get_runner_status(
        config=RunnerConfig(mode="auto", docker_image="personal-agent-skill-runner:test"),
        docker_available_checker=lambda: True,
        image_manager_factory=lambda _image: FakeImageManager(),
    )

    assert status.docker_daemon_available is True
    assert status.available is expected_available
    assert status.image_ready is expected_ready
    assert status.last_build_attempt == ("built" if image_status == "built" else "failed")
