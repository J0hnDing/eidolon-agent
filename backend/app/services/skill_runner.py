import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models import SkillRun
from app.schemas.manifest import SkillManifest
from app.services.docker_image_manager import DockerImageBuildError, DockerImageManager
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file


DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_DOCKER_IMAGE = "personal-agent-skill-runner:latest"
DEFAULT_DOCKER_MEMORY = "256m"
DEFAULT_DOCKER_CPUS = "1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "skill_cache"


class UnsupportedSkillPermissionError(ValueError):
    pass


class RunAlreadyFinalized(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_supported_permissions(manifest: SkillManifest) -> None:
    permissions = manifest.permissions
    if permissions.filesystem_read:
        raise UnsupportedSkillPermissionError("filesystem read permissions are not supported by the current runner")
    if permissions.secrets:
        raise UnsupportedSkillPermissionError("secret permissions are not supported by the current runner")
    if permissions.shell:
        raise UnsupportedSkillPermissionError("shell permissions are not supported by the current runner")

    allowed_writes = {"cache"}
    requested_writes = set()
    for path in permissions.filesystem_write:
        normalized = path.replace("\\", "/").removeprefix("./").rstrip("/")
        if Path(path).is_absolute() or path.replace("\\", "/").startswith("/") or ".." in Path(normalized).parts:
            raise UnsupportedSkillPermissionError("filesystem write paths must stay inside ./cache")
        requested_writes.add(normalized)
    if not requested_writes.issubset(allowed_writes):
        raise UnsupportedSkillPermissionError("filesystem writes are limited to ./cache")


@dataclass(frozen=True)
class RunnerConfig:
    mode: str = "auto"
    docker_image: str = DEFAULT_DOCKER_IMAGE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    memory_limit: str = DEFAULT_DOCKER_MEMORY
    cpu_limit: str = DEFAULT_DOCKER_CPUS
    runtime_root: Path = DEFAULT_RUNTIME_ROOT

    @classmethod
    def from_env(cls) -> "RunnerConfig":
        return cls(
            mode=os.getenv("PERSONAL_AGENT_RUNNER_MODE", "auto").strip().lower(),
            docker_image=os.getenv("PERSONAL_AGENT_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE),
            timeout_seconds=int(os.getenv("PERSONAL_AGENT_SKILL_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
            memory_limit=os.getenv("PERSONAL_AGENT_DOCKER_MEMORY", DEFAULT_DOCKER_MEMORY),
            cpu_limit=os.getenv("PERSONAL_AGENT_DOCKER_CPUS", DEFAULT_DOCKER_CPUS),
            runtime_root=Path(os.getenv("PERSONAL_AGENT_SKILL_RUNTIME_ROOT", str(DEFAULT_RUNTIME_ROOT))),
        )


@dataclass(frozen=True)
class RunnerStatus:
    mode: str
    selected_mode: str
    docker_available: bool
    available: bool
    detail: str
    image: str | None = None
    image_status: str | None = None
    image_detail: str | None = None
    image_build_log: str | None = None
    image_error: str | None = None


def is_docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def get_runner_status(
    config: RunnerConfig | None = None,
    docker_available_checker: Callable[[], bool] = is_docker_available,
) -> RunnerStatus:
    config = config or RunnerConfig.from_env()
    mode = config.mode
    if mode in {"local", "dev"}:
        return RunnerStatus(
            mode=mode,
            selected_mode="local",
            docker_available=docker_available_checker(),
            available=True,
            detail="Local dev runner is explicitly enabled. This is not a Docker sandbox.",
            image=config.docker_image,
            image_status="not_applicable",
            image_detail="Local dev mode does not use the Docker runner image.",
        )
    if mode not in {"auto", "docker"}:
        return RunnerStatus(
            mode=mode,
            selected_mode="docker",
            docker_available=False,
            available=False,
            detail="Unknown runner mode. Use auto, docker, local, or dev.",
            image=config.docker_image,
            image_status="unknown",
            image_detail="Runner mode is invalid, so Docker image status was not checked.",
        )

    docker_available = docker_available_checker()
    if docker_available:
        try:
            image_status = DockerImageManager(config.docker_image).get_status()
        except Exception as exc:
            return RunnerStatus(
                mode=mode,
                selected_mode="docker",
                docker_available=True,
                available=False,
                detail=f"Docker is available, but runner image status could not be checked: {exc}",
                image=config.docker_image,
                image_status="failed",
                image_detail="Docker runner image status check failed.",
                image_error=str(exc),
            )
        return RunnerStatus(
            mode=mode,
            selected_mode="docker",
            docker_available=True,
            available=True,
            detail=f"Docker sandbox runner is selected with image {config.docker_image}.",
            image=image_status.image,
            image_status=image_status.status,
            image_detail=image_status.detail,
            image_build_log=image_status.last_build_log,
            image_error=image_status.last_error,
        )
    return RunnerStatus(
        mode=mode,
        selected_mode="docker",
        docker_available=False,
        available=False,
        detail=(
            "Docker sandbox runner is selected, but Docker is unavailable. "
            "Set PERSONAL_AGENT_RUNNER_MODE=local only for explicit dev fallback."
        ),
        image=config.docker_image,
        image_status="unknown",
        image_detail="Docker is unavailable, so runner image status was not checked.",
    )


def get_skill_runner(db: Session, config: RunnerConfig | None = None):
    config = config or RunnerConfig.from_env()
    if config.mode in {"local", "dev"}:
        return LocalSkillRunner(db, timeout_seconds=config.timeout_seconds)
    return DockerSkillRunner(db, config=config)


class LocalSkillRunner:
    def __init__(self, db: Session, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.db = db
        self.timeout_seconds = timeout_seconds

    def run(self, skill_id: int, skill_dir: Path, input_json: dict[str, Any]) -> SkillRun:
        run = SkillRun(
            skill_id=skill_id,
            status="running",
            input_json=input_json,
            started_at=utc_now(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        skill_dir = skill_dir.resolve()
        try:
            manifest = self._load_manifest(skill_dir)
            if manifest.skill_type == "instruction":
                raise UnsupportedSkillPermissionError("instruction skills cannot be executed")
            validate_supported_permissions(manifest)
            entrypoint = self._resolve_entrypoint(skill_dir, manifest.entrypoint)
            self._run_tests(skill_dir, run)
            self._run_entrypoint(entrypoint, skill_dir, input_json, run)
        except (ManifestValidationError, UnsupportedSkillPermissionError, FileNotFoundError) as exc:
            self._finish_run(run, status="blocked", error_message=str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            self._finish_run(
                run,
                status="failed",
                stdout=stdout,
                stderr=stderr,
                error_message=f"Skill timed out after {self.timeout_seconds} seconds",
            )
        except RunAlreadyFinalized:
            pass
        except Exception as exc:
            self._finish_run(run, status="failed", error_message=str(exc))

        self.db.refresh(run)
        return run

    def _load_manifest(self, skill_dir: Path) -> SkillManifest:
        manifest_path = skill_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found at {manifest_path}")
        return validate_manifest_file(manifest_path)

    def _resolve_entrypoint(self, skill_dir: Path, entrypoint: str | None) -> Path:
        if entrypoint is None:
            raise FileNotFoundError("Executable skills require an entrypoint")
        resolved = (skill_dir / entrypoint).resolve()
        if not resolved.is_relative_to(skill_dir):
            raise FileNotFoundError("Skill entrypoint must stay inside the skill directory")
        if not resolved.exists():
            raise FileNotFoundError(f"Skill entrypoint not found at {resolved}")
        return resolved

    def _run_tests(self, skill_dir: Path, run: SkillRun) -> None:
        tests_dir = skill_dir / "tests"
        if not tests_dir.exists():
            self._finish_run(run, status="blocked", error_message="Skill tests directory is missing")
            raise RunAlreadyFinalized("Skill tests directory is missing")

        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_dir)],
            cwd=skill_dir,
            capture_output=True,
            text=True,
            env=self._skill_env(skill_dir),
            timeout=self.timeout_seconds,
            shell=False,
        )
        if result.returncode != 0:
            self._finish_run(
                run,
                status="blocked",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill tests failed; entrypoint was not run",
            )
            raise RunAlreadyFinalized("Skill tests failed; entrypoint was not run")

    def _run_entrypoint(
        self,
        entrypoint: Path,
        skill_dir: Path,
        input_json: dict[str, Any],
        run: SkillRun,
    ) -> None:
        result = subprocess.run(
            [sys.executable, str(entrypoint)],
            cwd=skill_dir,
            input=json.dumps(input_json),
            capture_output=True,
            text=True,
            env=self._skill_env(skill_dir),
            timeout=self.timeout_seconds,
            shell=False,
        )

        if result.returncode != 0:
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill exited with a non-zero status",
            )
            return

        try:
            output_json = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message=f"Skill stdout was not valid JSON: {exc.msg}",
            )
            return

        if not isinstance(output_json, dict):
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill stdout JSON must be an object",
            )
            return

        self._finish_run(
            run,
            status="succeeded",
            output_json=output_json,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
        )

    def _finish_run(
        self,
        run: SkillRun,
        status: str,
        output_json: dict[str, Any] | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
        error_message: str | None = None,
    ) -> None:
        run.status = status
        if output_json is not None:
            run.output_json = output_json
        if stdout is not None:
            run.stdout = stdout
        if stderr is not None:
            run.stderr = stderr
        if exit_code is not None:
            run.exit_code = exit_code
        if error_message is not None:
            run.error_message = error_message
        run.ended_at = utc_now()
        self.db.commit()

    def _skill_env(self, skill_dir: Path) -> dict[str, str]:
        env = os.environ.copy()
        deps_dir = skill_dir / ".deps"
        if deps_dir.is_dir():
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = str(deps_dir) if not existing else f"{deps_dir}{os.pathsep}{existing}"
        return env


class DockerSkillRunner:
    def __init__(
        self,
        db: Session,
        config: RunnerConfig | None = None,
        docker_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        docker_available_checker: Callable[[], bool] = is_docker_available,
        image_manager: DockerImageManager | None = None,
    ) -> None:
        self.db = db
        self.config = config or RunnerConfig.from_env()
        self.timeout_seconds = self.config.timeout_seconds
        self.docker_runner = docker_runner
        self.docker_available_checker = docker_available_checker
        self.image_manager = image_manager or DockerImageManager(
            self.config.docker_image,
            docker_runner=docker_runner,
        )

    def run(self, skill_id: int, skill_dir: Path, input_json: dict[str, Any]) -> SkillRun:
        run = SkillRun(
            skill_id=skill_id,
            status="running",
            input_json=input_json,
            started_at=utc_now(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        skill_dir = skill_dir.resolve()
        try:
            if not self.docker_available_checker():
                raise UnsupportedSkillPermissionError(
                    "Docker sandbox runner is selected, but Docker is unavailable. "
                    "Set PERSONAL_AGENT_RUNNER_MODE=local only for explicit dev fallback."
                )
            manifest = self._load_manifest(skill_dir)
            if manifest.skill_type == "instruction":
                raise UnsupportedSkillPermissionError("instruction skills cannot be executed")
            validate_supported_permissions(manifest)
            entrypoint = self._resolve_entrypoint(skill_dir, manifest.entrypoint)
            self.image_manager.ensure_image()
            cache_dir = self._prepare_cache_dir(skill_id, skill_dir)
            self._run_tests(skill_dir, cache_dir, manifest, run)
            self._run_entrypoint(entrypoint, skill_dir, cache_dir, manifest, input_json, run)
        except DockerImageBuildError as exc:
            self._finish_run(run, status="blocked", error_message=str(exc))
        except (ManifestValidationError, UnsupportedSkillPermissionError, FileNotFoundError) as exc:
            self._finish_run(run, status="blocked", error_message=str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            self._finish_run(
                run,
                status="failed",
                stdout=stdout,
                stderr=stderr,
                error_message=f"Skill timed out after {self.timeout_seconds} seconds",
            )
        except RunAlreadyFinalized:
            pass
        except Exception as exc:
            self._finish_run(run, status="failed", error_message=str(exc))

        self.db.refresh(run)
        return run

    def build_base_docker_command(self, skill_dir: Path, cache_dir: Path, manifest: SkillManifest | None = None) -> list[str]:
        network_mode = "bridge" if manifest is not None and manifest.permissions.network else "none"
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            network_mode,
            "--memory",
            self.config.memory_limit,
            "--cpus",
            self.config.cpu_limit,
            "-i",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
            "-e",
            "PYTHONPATH=/skill/.deps",
            "-v",
            f"{skill_dir.resolve()}:/skill:ro",
            "-v",
            f"{cache_dir.resolve()}:/skill/cache:rw",
            "-w",
            "/skill",
            self.config.docker_image,
        ]

    def build_pytest_command(self, skill_dir: Path, cache_dir: Path, manifest: SkillManifest | None = None) -> list[str]:
        return self.build_base_docker_command(skill_dir, cache_dir, manifest) + ["python", "-m", "pytest", "/skill/tests"]

    def build_entrypoint_command(
        self,
        skill_dir: Path,
        cache_dir: Path,
        entrypoint: Path,
        manifest: SkillManifest | None = None,
    ) -> list[str]:
        relative_entrypoint = entrypoint.relative_to(skill_dir).as_posix()
        return self.build_base_docker_command(skill_dir, cache_dir, manifest) + ["python", f"/skill/{relative_entrypoint}"]

    def _load_manifest(self, skill_dir: Path) -> SkillManifest:
        manifest_path = skill_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found at {manifest_path}")
        return validate_manifest_file(manifest_path)

    def _resolve_entrypoint(self, skill_dir: Path, entrypoint: str | None) -> Path:
        if entrypoint is None:
            raise FileNotFoundError("Executable skills require an entrypoint")
        resolved = (skill_dir / entrypoint).resolve()
        if not resolved.is_relative_to(skill_dir):
            raise FileNotFoundError("Skill entrypoint must stay inside the skill directory")
        if not resolved.exists():
            raise FileNotFoundError(f"Skill entrypoint not found at {resolved}")
        return resolved

    def _prepare_cache_dir(self, skill_id: int, skill_dir: Path) -> Path:
        cache_dir = (self.config.runtime_root / f"skill_{skill_id}" / "cache").resolve()
        runtime_root = self.config.runtime_root.resolve()
        if not cache_dir.is_relative_to(runtime_root):
            raise UnsupportedSkillPermissionError("Runtime cache directory must stay under the configured runtime root")
        cache_dir.mkdir(parents=True, exist_ok=True)

        mountpoint = (skill_dir / "cache").resolve()
        if not mountpoint.is_relative_to(skill_dir):
            raise UnsupportedSkillPermissionError("Skill cache mountpoint must stay inside the skill directory")
        if mountpoint.is_symlink() or (mountpoint.exists() and not mountpoint.is_dir()):
            raise UnsupportedSkillPermissionError("Skill cache mountpoint must be a regular directory")
        mountpoint.mkdir(exist_ok=True)
        return cache_dir

    def _run_tests(self, skill_dir: Path, cache_dir: Path, manifest: SkillManifest, run: SkillRun) -> None:
        tests_dir = skill_dir / "tests"
        if not tests_dir.exists():
            self._finish_run(run, status="blocked", error_message="Skill tests directory is missing")
            raise RunAlreadyFinalized("Skill tests directory is missing")

        result = self.docker_runner(
            self.build_pytest_command(skill_dir, cache_dir, manifest),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )
        if result.returncode != 0:
            self._finish_run(
                run,
                status="blocked",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill tests failed; entrypoint was not run",
            )
            raise RunAlreadyFinalized("Skill tests failed; entrypoint was not run")

    def _run_entrypoint(
        self,
        entrypoint: Path,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest,
        input_json: dict[str, Any],
        run: SkillRun,
    ) -> None:
        result = self.docker_runner(
            self.build_entrypoint_command(skill_dir, cache_dir, entrypoint, manifest),
            input=json.dumps(input_json),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )

        if result.returncode != 0:
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill exited with a non-zero status",
            )
            return

        try:
            output_json = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message=f"Skill stdout was not valid JSON: {exc.msg}",
            )
            return

        if not isinstance(output_json, dict):
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill stdout JSON must be an object",
            )
            return

        self._finish_run(
            run,
            status="succeeded",
            output_json=output_json,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
        )

    def _finish_run(
        self,
        run: SkillRun,
        status: str,
        output_json: dict[str, Any] | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
        error_message: str | None = None,
    ) -> None:
        run.status = status
        if output_json is not None:
            run.output_json = output_json
        if stdout is not None:
            run.stdout = stdout
        if stderr is not None:
            run.stderr = stderr
        if exit_code is not None:
            run.exit_code = exit_code
        if error_message is not None:
            run.error_message = error_message
        run.ended_at = utc_now()
        self.db.commit()


DevSkillRunner = LocalSkillRunner
SkillRunner = LocalSkillRunner
