import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TRUSTED_DOCKERFILE = PROJECT_ROOT / "backend" / "docker" / "skill-runner.Dockerfile"
TRUSTED_BUILD_CONTEXT = PROJECT_ROOT
DEFAULT_METADATA_PATH = PROJECT_ROOT / "runtime" / "docker_runner_build.json"


class DockerImageBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class DockerImageStatus:
    image: str
    status: str
    dockerfile_hash: str | None
    last_successful_hash: str | None
    detail: str
    last_build_log: str | None = None
    last_error: str | None = None


class DockerImageManager:
    def __init__(
        self,
        image_name: str,
        docker_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        project_root: Path = PROJECT_ROOT,
        dockerfile: Path = TRUSTED_DOCKERFILE,
        metadata_path: Path = DEFAULT_METADATA_PATH,
    ) -> None:
        if not image_name.strip():
            raise ValueError("Docker image name cannot be empty")
        self.image_name = image_name
        self.docker_runner = docker_runner
        self.project_root = project_root.resolve()
        self.dockerfile = dockerfile.resolve()
        self.metadata_path = metadata_path.resolve()
        self._assert_trusted_paths()

    def ensure_image(self) -> DockerImageStatus:
        status = self.get_status()
        if status.status == "built":
            return status
        return self._build_image(status)

    def get_status(self) -> DockerImageStatus:
        dockerfile_hash = self._dockerfile_hash()
        metadata = self._read_metadata()
        last_successful_hash = metadata.get("dockerfile_hash") if metadata.get("status") == "built" else None
        image_exists = self._image_exists()
        last_build_log = metadata.get("last_build_log")
        last_error = metadata.get("last_error")

        if metadata.get("status") == "failed":
            return DockerImageStatus(
                image=self.image_name,
                status="failed",
                dockerfile_hash=dockerfile_hash,
                last_successful_hash=last_successful_hash,
                detail="The last trusted Docker runner image build failed.",
                last_build_log=last_build_log,
                last_error=last_error,
            )
        if not image_exists:
            return DockerImageStatus(
                image=self.image_name,
                status="missing",
                dockerfile_hash=dockerfile_hash,
                last_successful_hash=last_successful_hash,
                detail="Docker runner image is missing and will be built automatically before the next run.",
                last_build_log=last_build_log,
                last_error=last_error,
            )
        if last_successful_hash != dockerfile_hash:
            return DockerImageStatus(
                image=self.image_name,
                status="outdated",
                dockerfile_hash=dockerfile_hash,
                last_successful_hash=last_successful_hash,
                detail="Docker runner image exists, but the trusted Dockerfile changed since the last recorded build.",
                last_build_log=last_build_log,
                last_error=last_error,
            )
        return DockerImageStatus(
            image=self.image_name,
            status="built",
            dockerfile_hash=dockerfile_hash,
            last_successful_hash=last_successful_hash,
            detail="Docker runner image is built from the current trusted Dockerfile.",
            last_build_log=last_build_log,
            last_error=last_error,
        )

    def build_command(self) -> list[str]:
        return [
            "docker",
            "build",
            "-f",
            str(self.dockerfile),
            "-t",
            self.image_name,
            str(self.project_root),
        ]

    def inspect_command(self) -> list[str]:
        return ["docker", "image", "inspect", self.image_name]

    def _build_image(self, previous_status: DockerImageStatus) -> DockerImageStatus:
        dockerfile_hash = previous_status.dockerfile_hash or self._dockerfile_hash()
        self._write_metadata(
            {
                "status": "rebuilding",
                "image": self.image_name,
                "dockerfile": str(self.dockerfile),
                "build_context": str(self.project_root),
                "dockerfile_hash": dockerfile_hash,
                "started_at": datetime.now(UTC).isoformat(),
                "last_build_log": previous_status.last_build_log,
                "last_error": None,
            }
        )
        result = self.docker_runner(
            self.build_command(),
            capture_output=True,
            text=True,
            shell=False,
        )
        combined_log = "\n".join(part for part in [result.stdout, result.stderr] if part)
        finished_at = datetime.now(UTC).isoformat()
        if result.returncode != 0:
            self._write_metadata(
                {
                    "status": "failed",
                    "image": self.image_name,
                    "dockerfile": str(self.dockerfile),
                    "build_context": str(self.project_root),
                    "dockerfile_hash": dockerfile_hash,
                    "started_at": None,
                    "finished_at": finished_at,
                    "last_build_log": combined_log,
                    "last_error": "Trusted Docker runner image build failed.",
                }
            )
            raise DockerImageBuildError(f"Trusted Docker runner image build failed.\n{combined_log}".strip())

        self._write_metadata(
            {
                "status": "built",
                "image": self.image_name,
                "dockerfile": str(self.dockerfile),
                "build_context": str(self.project_root),
                "dockerfile_hash": dockerfile_hash,
                "started_at": None,
                "finished_at": finished_at,
                "last_build_log": combined_log,
                "last_error": None,
            }
        )
        return self.get_status()

    def _image_exists(self) -> bool:
        result = self.docker_runner(
            self.inspect_command(),
            capture_output=True,
            text=True,
            shell=False,
        )
        return result.returncode == 0

    def _dockerfile_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.dockerfile.read_bytes())
        return digest.hexdigest()

    def _read_metadata(self) -> dict[str, Any]:
        if not self.metadata_path.exists():
            return {}
        try:
            raw = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_metadata(self, payload: dict[str, Any]) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _assert_trusted_paths(self) -> None:
        if self.project_root != TRUSTED_BUILD_CONTEXT.resolve():
            raise ValueError("Docker runner build context must be the project root")
        if self.dockerfile != TRUSTED_DOCKERFILE.resolve():
            raise ValueError("Docker runner Dockerfile must be backend/docker/skill-runner.Dockerfile")
