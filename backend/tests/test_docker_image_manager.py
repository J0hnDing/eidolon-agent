import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.services.docker_image_manager import (
    PROJECT_ROOT,
    TRUSTED_BUILD_CONTEXT,
    TRUSTED_DOCKERFILE,
    DockerImageBuildError,
    DockerImageManager,
)


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    def __init__(self, image_exists: bool = True, build_returncode: int = 0) -> None:
        self.image_exists = image_exists
        self.build_returncode = build_returncode
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return completed(stdout="[]", returncode=0 if self.image_exists else 1)
        if command[:2] == ["docker", "build"]:
            if self.build_returncode == 0:
                self.image_exists = True
                return completed(stdout="build ok")
            return completed(stderr="build failed", returncode=self.build_returncode)
        return completed()


def manager(tmp_path: Path, fake_docker: FakeDocker) -> DockerImageManager:
    return DockerImageManager(
        image_name="personal-agent-skill-runner:test",
        docker_runner=fake_docker,
        metadata_path=tmp_path / "docker_runner_build.json",
    )


def write_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_image_exists_with_current_metadata_does_not_build(tmp_path: Path) -> None:
    fake_docker = FakeDocker(image_exists=True)
    image_manager = manager(tmp_path, fake_docker)
    current_hash = image_manager._dockerfile_hash()
    write_metadata(
        tmp_path / "docker_runner_build.json",
        {
            "status": "built",
            "image": "personal-agent-skill-runner:test",
            "dockerfile_hash": current_hash,
        },
    )

    status = image_manager.ensure_image()

    assert status.status == "built"
    assert ["docker", "build"] not in [command[:2] for command in fake_docker.commands]


def test_missing_image_builds_automatically(tmp_path: Path) -> None:
    fake_docker = FakeDocker(image_exists=False)
    image_manager = manager(tmp_path, fake_docker)

    status = image_manager.ensure_image()

    assert status.status == "built"
    assert any(command[:2] == ["docker", "build"] for command in fake_docker.commands)
    metadata = json.loads((tmp_path / "docker_runner_build.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "built"
    assert metadata["last_build_log"] == "build ok"


def test_dockerfile_hash_change_rebuilds_automatically(tmp_path: Path) -> None:
    fake_docker = FakeDocker(image_exists=True)
    image_manager = manager(tmp_path, fake_docker)
    write_metadata(
        tmp_path / "docker_runner_build.json",
        {
            "status": "built",
            "image": "personal-agent-skill-runner:test",
            "dockerfile_hash": "old-hash",
        },
    )

    status = image_manager.ensure_image()

    assert status.status == "built"
    assert any(command[:2] == ["docker", "build"] for command in fake_docker.commands)
    assert status.last_successful_hash == image_manager._dockerfile_hash()


def test_build_failure_records_metadata_and_blocks(tmp_path: Path) -> None:
    fake_docker = FakeDocker(image_exists=False, build_returncode=1)
    image_manager = manager(tmp_path, fake_docker)

    with pytest.raises(DockerImageBuildError, match="Trusted Docker runner image build failed"):
        image_manager.ensure_image()

    metadata = json.loads((tmp_path / "docker_runner_build.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["last_error"] == "Trusted Docker runner image build failed."
    assert "build failed" in metadata["last_build_log"]


def test_failed_legacy_metadata_with_existing_image_is_unverified_and_rebuilt(tmp_path: Path) -> None:
    fake_docker = FakeDocker(image_exists=True)
    image_manager = manager(tmp_path, fake_docker)
    write_metadata(
        tmp_path / "docker_runner_build.json",
        {
            "status": "failed",
            "image": "personal-agent-skill-runner:test",
            "dockerfile_hash": image_manager._dockerfile_hash(),
            "last_error": "Trusted Docker runner image build failed.",
        },
    )

    before = image_manager.get_status()
    after = image_manager.ensure_image()

    assert before.status == "unverified"
    assert before.last_build_status == "failed"
    assert after.status == "built"
    assert any(command[:2] == ["docker", "build"] for command in fake_docker.commands)


def test_failed_rebuild_preserves_last_successful_image_hash(tmp_path: Path) -> None:
    fake_docker = FakeDocker(image_exists=True, build_returncode=1)
    image_manager = manager(tmp_path, fake_docker)
    write_metadata(
        tmp_path / "docker_runner_build.json",
        {
            "status": "built",
            "image": "personal-agent-skill-runner:test",
            "dockerfile_hash": "previous-trusted-hash",
        },
    )

    with pytest.raises(DockerImageBuildError):
        image_manager.ensure_image()

    metadata = json.loads((tmp_path / "docker_runner_build.json").read_text(encoding="utf-8"))
    assert metadata["last_successful_hash"] == "previous-trusted-hash"
    assert image_manager.get_status().status == "outdated"


def test_build_command_uses_only_trusted_inputs(tmp_path: Path) -> None:
    fake_docker = FakeDocker(image_exists=False)
    image_manager = manager(tmp_path, fake_docker)

    command = image_manager.build_command()

    assert command == [
        "docker",
        "build",
        "-f",
        str(TRUSTED_DOCKERFILE.resolve()),
        "-t",
        "personal-agent-skill-runner:test",
        str(TRUSTED_BUILD_CONTEXT.resolve()),
    ]
    assert "--build-arg" not in command
    assert str(PROJECT_ROOT.resolve()) == command[-1]


def test_untrusted_dockerfile_or_context_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="build context must be the project root"):
        DockerImageManager(
            image_name="personal-agent-skill-runner:test",
            project_root=tmp_path,
            metadata_path=tmp_path / "metadata.json",
        )

    with pytest.raises(ValueError, match="Dockerfile must be backend/docker/skill-runner.Dockerfile"):
        DockerImageManager(
            image_name="personal-agent-skill-runner:test",
            dockerfile=tmp_path / "Dockerfile",
            metadata_path=tmp_path / "metadata.json",
        )
