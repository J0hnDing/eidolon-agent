import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import SkillRun
from app.schemas.manifest import SkillManifest
from app.services.docker_image_manager import DockerImageBuildError, DockerImageManager
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_DOCKER_IMAGE = "personal-agent-skill-runner:latest"
DEFAULT_DOCKER_MEMORY = "256m"
DEFAULT_DOCKER_CPUS = "1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "skill_cache"
DEFAULT_LOCAL_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_DOCKER_BACKEND_URL = "http://host.docker.internal:8000"


class UnsupportedSkillPermissionError(ValueError):
    pass


class RunAlreadyFinalized(RuntimeError):
    pass


def _reported_run_outcome(output_json: dict[str, Any]) -> tuple[str, str | None]:
    reported_status = output_json.get("status")
    if reported_status not in {"partial", "failed"}:
        return "succeeded", None

    failures = output_json.get("failures")
    messages: list[str] = []
    if isinstance(failures, list):
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            message = failure.get("message")
            if isinstance(message, str) and message.strip() and message.strip() not in messages:
                messages.append(message.strip())

    prefix = f"Skill reported {reported_status} output"
    if not messages:
        return reported_status, prefix
    shown = messages[:3]
    remainder = len(messages) - len(shown)
    detail = "; ".join(shown)
    if remainder:
        detail += f" (+{remainder} more distinct failure{'s' if remainder != 1 else ''})"
    return reported_status, f"{prefix}: {detail}"


def _final_run_outcome(run: SkillRun, output_json: dict[str, Any]) -> tuple[str, str | None]:
    status, error_message = _reported_run_outcome(output_json)
    has_failed_codex_call = any(
        invocation.get("status") == "failed"
        for invocation in (run.codex_invocations_json or [])
        if isinstance(invocation, dict)
    )
    if status == "succeeded" and has_failed_codex_call:
        return "partial", "Skill completed after one or more runtime Codex calls failed"
    return status, error_message


def _merge_error_message(existing: str | None, current: str | None) -> str | None:
    if not existing:
        return current
    if not current or current in existing:
        return existing
    return f"{existing}\n{current}"


def _process_exit_error(returncode: int) -> str:
    known_exits = {
        126: "Skill could not be executed (exit code 126)",
        127: "Skill command was not found (exit code 127)",
        130: "Skill was interrupted by SIGINT (exit code 130)",
        137: "Skill was forcibly killed by SIGKILL (exit code 137)",
        139: "Skill crashed with a segmentation fault (exit code 139)",
        143: "Skill was terminated by SIGTERM (exit code 143)",
    }
    return known_exits.get(returncode, f"Skill exited with code {returncode}")


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_supported_permissions(manifest: SkillManifest) -> None:
    permissions = manifest.permissions
    requested_reads = {
        path.replace("\\", "/").removeprefix("./").rstrip("/")
        for path in permissions.filesystem_read
    }
    if not requested_reads.issubset({"cache"}):
        raise UnsupportedSkillPermissionError(
            "filesystem read permissions are limited to the skill's own ./cache directory"
        )
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
class FunctionRunContext:
    version_id: int | None = None
    invocation_source: str = "internal"
    caller_skill_id: int | None = None
    caller_version_id: int | None = None
    parent_run_id: int | None = None
    source_schedule_id: int | None = None
    schedule_occurrence_key: str | None = None
    scheduled_for_at: datetime | None = None
    schedule_trigger: str | None = None
    web_app_instance_id: str | None = None
    initiating_action: str | None = None
    capability_token: str | None = None

    @property
    def capability_token_hash(self) -> str | None:
        if self.capability_token is None:
            return None
        return hashlib.sha256(self.capability_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunnerStatus:
    mode: str
    selected_mode: str
    docker_available: bool
    docker_daemon_available: bool
    available: bool
    detail: str
    image: str | None = None
    image_status: str | None = None
    image_ready: bool = False
    last_build_attempt: str | None = None
    last_build_at: str | None = None
    image_detail: str | None = None
    image_build_log: str | None = None
    image_error: str | None = None


def is_docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
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
    image_manager_factory: Callable[[str], DockerImageManager] = DockerImageManager,
) -> RunnerStatus:
    config = config or RunnerConfig.from_env()
    mode = config.mode
    if mode in {"local", "dev"}:
        docker_daemon_available = docker_available_checker()
        return RunnerStatus(
            mode=mode,
            selected_mode="local",
            docker_available=docker_daemon_available,
            docker_daemon_available=docker_daemon_available,
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
            docker_daemon_available=False,
            available=False,
            detail="Unknown runner mode. Use auto, docker, local, or dev.",
            image=config.docker_image,
            image_status="unknown",
            image_detail="Runner mode is invalid, so Docker image status was not checked.",
        )

    docker_available = docker_available_checker()
    if docker_available:
        try:
            image_status = image_manager_factory(config.docker_image).get_status()
        except Exception as exc:
            return RunnerStatus(
                mode=mode,
                selected_mode="docker",
                docker_available=True,
                docker_daemon_available=True,
                available=False,
                detail=f"Docker is available, but runner image status could not be checked: {exc}",
                image=config.docker_image,
                image_status="failed",
                image_ready=False,
                image_detail="Docker runner image status check failed.",
                image_error=str(exc),
            )
        image_ready = image_status.status == "built"
        detail = (
            f"Docker sandbox runner is ready with image {config.docker_image}."
            if image_ready
            else (
                f"Docker daemon is available, but runner image {config.docker_image} is not ready. "
                "It will be rebuilt automatically before the next run."
            )
        )
        return RunnerStatus(
            mode=mode,
            selected_mode="docker",
            docker_available=True,
            docker_daemon_available=True,
            available=image_ready,
            detail=detail,
            image=image_status.image,
            image_status=image_status.status,
            image_ready=image_ready,
            last_build_attempt=image_status.last_build_status,
            last_build_at=image_status.last_build_at,
            image_detail=image_status.detail,
            image_build_log=image_status.last_build_log,
            image_error=image_status.last_error,
        )
    return RunnerStatus(
        mode=mode,
        selected_mode="docker",
        docker_available=False,
        docker_daemon_available=False,
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

    def run(
        self,
        skill_id: int,
        skill_dir: Path,
        input_json: dict[str, Any],
        context: FunctionRunContext | None = None,
    ) -> SkillRun:
        context = context or FunctionRunContext()
        run = SkillRun(
            skill_id=skill_id,
            status="running",
            input_json=input_json,
            started_at=utc_now(),
            version_id=context.version_id,
            invocation_source=context.invocation_source,
            caller_skill_id=context.caller_skill_id,
            caller_version_id=context.caller_version_id,
            parent_run_id=context.parent_run_id,
            source_schedule_id=context.source_schedule_id,
            schedule_occurrence_key=context.schedule_occurrence_key,
            scheduled_for_at=context.scheduled_for_at,
            schedule_trigger=context.schedule_trigger,
            web_app_instance_id=context.web_app_instance_id,
            initiating_action=context.initiating_action,
            function_capability_token_hash=context.capability_token_hash,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        skill_dir = skill_dir.resolve()
        try:
            manifest = self._load_manifest(skill_dir)
            if manifest.runtime not in {"function", "service"}:
                raise UnsupportedSkillPermissionError(
                    "web_app skills must use the persistent web application runtime"
                )
            validate_supported_permissions(manifest)
            entrypoint = self._resolve_entrypoint(skill_dir, manifest.entrypoint)
            self._run_tests(skill_dir, run)
            self._run_entrypoint(
                entrypoint,
                skill_dir,
                input_json,
                run,
                context.capability_token,
                context.schedule_occurrence_key,
            )
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
            raise FileNotFoundError("Skills require an entrypoint")
        resolved = (skill_dir / entrypoint).resolve()
        if not resolved.is_relative_to(skill_dir):
            raise FileNotFoundError("Skill entrypoint must stay inside the skill directory")
        if not resolved.exists():
            raise FileNotFoundError(f"Skill entrypoint not found at {resolved}")
        return resolved

    def _run_tests(self, skill_dir: Path, run: SkillRun, capability_token: str | None = None) -> None:
        tests_dir = skill_dir / "tests"
        if not tests_dir.exists():
            self._finish_run(run, status="blocked", error_message="Skill tests directory is missing")
            raise RunAlreadyFinalized("Skill tests directory is missing")

        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_dir)],
            cwd=skill_dir,
            capture_output=True,
            text=True,
            env=self._skill_env(skill_dir, run.skill_id, capability_token),
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
        capability_token: str | None = None,
        schedule_occurrence_key: str | None = None,
    ) -> None:
        result = subprocess.run(
            [sys.executable, str(entrypoint)],
            cwd=skill_dir,
            input=json.dumps(input_json),
            capture_output=True,
            text=True,
            env=self._skill_env(
                skill_dir,
                run.skill_id,
                capability_token,
                schedule_occurrence_key,
            ),
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
                error_message=_process_exit_error(result.returncode),
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

        self.db.refresh(run)
        run_status, error_message = _final_run_outcome(run, output_json)
        self._finish_run(
            run,
            status=run_status,
            output_json=output_json,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            error_message=error_message,
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
        run.error_message = _merge_error_message(run.error_message, error_message)
        run.ended_at = utc_now()
        self.db.commit()

    def _skill_env(
        self,
        skill_dir: Path,
        skill_id: int,
        capability_token: str | None = None,
        schedule_occurrence_key: str | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PERSONAL_AGENT_SKILL_ID", str(skill_id))
        env.setdefault("PERSONAL_AGENT_BACKEND_URL", os.getenv("PERSONAL_AGENT_BACKEND_URL", DEFAULT_LOCAL_BACKEND_URL))
        if capability_token is not None:
            env["PERSONAL_AGENT_FUNCTION_CAPABILITY"] = capability_token
        if schedule_occurrence_key is not None:
            env["PERSONAL_AGENT_SCHEDULE_IDEMPOTENCY_KEY"] = schedule_occurrence_key
        deps_dir = skill_dir / ".deps"
        python_paths = [str(Path(__file__).resolve().parents[2])]
        if deps_dir.is_dir():
            python_paths.insert(0, str(deps_dir))
        existing = env.get("PYTHONPATH")
        if existing:
            python_paths.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
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

    def run(
        self,
        skill_id: int,
        skill_dir: Path,
        input_json: dict[str, Any],
        context: FunctionRunContext | None = None,
    ) -> SkillRun:
        context = context or FunctionRunContext()
        run = SkillRun(
            skill_id=skill_id,
            status="running",
            input_json=input_json,
            started_at=utc_now(),
            version_id=context.version_id,
            invocation_source=context.invocation_source,
            caller_skill_id=context.caller_skill_id,
            caller_version_id=context.caller_version_id,
            parent_run_id=context.parent_run_id,
            source_schedule_id=context.source_schedule_id,
            schedule_occurrence_key=context.schedule_occurrence_key,
            scheduled_for_at=context.scheduled_for_at,
            schedule_trigger=context.schedule_trigger,
            web_app_instance_id=context.web_app_instance_id,
            initiating_action=context.initiating_action,
            function_capability_token_hash=context.capability_token_hash,
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
            if manifest.runtime not in {"function", "service"}:
                raise UnsupportedSkillPermissionError(
                    "web_app skills must use the persistent web application runtime"
                )
            validate_supported_permissions(manifest)
            entrypoint = self._resolve_entrypoint(skill_dir, manifest.entrypoint)
            self.image_manager.ensure_image()
            cache_dir = self._prepare_cache_dir(skill_id, skill_dir)
            self._run_tests(skill_dir, cache_dir, manifest, run)
            self._run_entrypoint(
                entrypoint,
                skill_dir,
                cache_dir,
                manifest,
                input_json,
                run,
                context.capability_token,
                context.schedule_occurrence_key,
            )
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

    def build_base_docker_command(
        self,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest | None = None,
        skill_id: int | None = None,
        capability_token: str | None = None,
        network_mode_override: str | None = None,
        backend_url_override: str | None = None,
        schedule_occurrence_key: str | None = None,
    ) -> list[str]:
        network_mode = network_mode_override or (
            "bridge" if manifest is not None and manifest.permissions.network else "none"
        )
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            network_mode,
            "--add-host",
            "api.github.com:127.0.0.1",
            "--add-host",
            "github.com:127.0.0.1",
            "--add-host",
            "api.notion.com:127.0.0.1",
            "--add-host",
            "accounts.google.com:127.0.0.1",
            "--add-host",
            "gmail.googleapis.com:127.0.0.1",
            "--add-host",
            "oauth2.googleapis.com:127.0.0.1",
            "--add-host",
            "openidconnect.googleapis.com:127.0.0.1",
            "--add-host",
            "www.googleapis.com:127.0.0.1",
            "--add-host",
            "api.telegram.org:127.0.0.1",
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
            "PYTHONPATH=/skill/.deps:/runtime",
            "-e",
            "PERSONAL_AGENT_BACKEND_URL="
            + (
                backend_url_override
                or os.getenv(
                    "PERSONAL_AGENT_DOCKER_BACKEND_URL",
                    os.getenv("PERSONAL_AGENT_BACKEND_URL", DEFAULT_DOCKER_BACKEND_URL),
                )
            ),
            *(["-e", f"PERSONAL_AGENT_SKILL_ID={skill_id}"] if skill_id is not None else []),
            *(
                ["-e", f"PERSONAL_AGENT_FUNCTION_CAPABILITY={capability_token}"]
                if capability_token is not None
                else []
            ),
            *(
                ["-e", f"PERSONAL_AGENT_SCHEDULE_IDEMPOTENCY_KEY={schedule_occurrence_key}"]
                if schedule_occurrence_key is not None
                else []
            ),
            "-v",
            f"{skill_dir.resolve()}:/skill:ro",
            "-v",
            f"{cache_dir.resolve()}:/skill/cache:rw",
            "-w",
            "/skill",
            self.config.docker_image,
        ]
        return command

    def build_pytest_command(
        self,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest | None = None,
        skill_id: int | None = None,
        capability_token: str | None = None,
        network_mode_override: str | None = None,
        backend_url_override: str | None = None,
        schedule_occurrence_key: str | None = None,
    ) -> list[str]:
        return self.build_base_docker_command(
            skill_dir,
            cache_dir,
            manifest,
            skill_id,
            capability_token,
            network_mode_override,
            backend_url_override,
            schedule_occurrence_key,
        ) + ["python", "-m", "pytest", "/skill/tests"]

    def build_entrypoint_command(
        self,
        skill_dir: Path,
        cache_dir: Path,
        entrypoint: Path,
        manifest: SkillManifest | None = None,
        skill_id: int | None = None,
        capability_token: str | None = None,
        network_mode_override: str | None = None,
        backend_url_override: str | None = None,
        schedule_occurrence_key: str | None = None,
    ) -> list[str]:
        relative_entrypoint = entrypoint.relative_to(skill_dir).as_posix()
        return self.build_base_docker_command(
            skill_dir,
            cache_dir,
            manifest,
            skill_id,
            capability_token,
            network_mode_override,
            backend_url_override,
            schedule_occurrence_key,
        ) + ["python", f"/skill/{relative_entrypoint}"]

    def _load_manifest(self, skill_dir: Path) -> SkillManifest:
        manifest_path = skill_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found at {manifest_path}")
        return validate_manifest_file(manifest_path)

    def _resolve_entrypoint(self, skill_dir: Path, entrypoint: str | None) -> Path:
        if entrypoint is None:
            raise FileNotFoundError("Skills require an entrypoint")
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

    def _run_tests(
        self,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest,
        run: SkillRun,
        capability_token: str | None = None,
    ) -> None:
        tests_dir = skill_dir / "tests"
        if not tests_dir.exists():
            self._finish_run(run, status="blocked", error_message="Skill tests directory is missing")
            raise RunAlreadyFinalized("Skill tests directory is missing")

        result = self.docker_runner(
            self.build_pytest_command(
                skill_dir,
                cache_dir,
                manifest,
                run.skill_id,
                capability_token,
            ),
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
        capability_token: str | None = None,
        schedule_occurrence_key: str | None = None,
    ) -> None:
        relay: tuple[str, str] | None = None
        try:
            if capability_token is not None and not manifest.permissions.network:
                relay = self._start_function_capability_relay(run.id)
            result = self.docker_runner(
                self.build_entrypoint_command(
                    skill_dir,
                    cache_dir,
                    entrypoint,
                    manifest,
                    run.skill_id,
                    capability_token,
                    relay[0] if relay is not None else None,
                    f"http://{relay[1]}:8000" if relay is not None else None,
                    schedule_occurrence_key,
                ),
                input=json.dumps(input_json),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )
        finally:
            if relay is not None:
                self._stop_function_capability_relay(*relay)

        if result.returncode != 0:
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message=_process_exit_error(result.returncode),
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

        self.db.refresh(run)
        run_status, error_message = _final_run_outcome(run, output_json)
        self._finish_run(
            run,
            status=run_status,
            output_json=output_json,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            error_message=error_message,
        )

    def _start_function_capability_relay(self, run_id: int) -> tuple[str, str]:
        suffix = uuid4().hex[:10]
        network_name = f"personal-agent-function-{run_id}-{suffix}"
        relay_name = f"personal-agent-function-relay-{run_id}-{suffix}"
        create_network = self.docker_runner(
            ["docker", "network", "create", "--internal", network_name],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        if create_network.returncode != 0:
            raise UnsupportedSkillPermissionError(
                create_network.stderr.strip() or "Could not create the private function capability network"
            )
        try:
            start_relay = self.docker_runner(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    relay_name,
                    "--network",
                    "bridge",
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    "-e",
                    "PERSONAL_AGENT_RELAY_BACKEND_URL="
                    + os.getenv(
                        "PERSONAL_AGENT_DOCKER_BACKEND_URL",
                        os.getenv("PERSONAL_AGENT_BACKEND_URL", DEFAULT_DOCKER_BACKEND_URL),
                    ),
                    self.config.docker_image,
                    "python",
                    "/runtime/function_runtime_relay.py",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
            if start_relay.returncode != 0:
                raise UnsupportedSkillPermissionError(
                    start_relay.stderr.strip() or "Could not start the trusted function capability relay"
                )
            connect_relay = self.docker_runner(
                ["docker", "network", "connect", "--alias", relay_name, network_name, relay_name],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
            if connect_relay.returncode != 0:
                raise UnsupportedSkillPermissionError(
                    connect_relay.stderr.strip() or "Could not attach the function capability relay"
                )
            for _attempt in range(20):
                ready = self.docker_runner(
                    [
                        "docker",
                        "exec",
                        relay_name,
                        "python",
                        "-c",
                        (
                            "from urllib.request import urlopen; "
                            "urlopen('http://127.0.0.1:8000/health', timeout=1).read()"
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    shell=False,
                )
                if ready.returncode == 0:
                    return network_name, relay_name
                time.sleep(0.1)
            raise UnsupportedSkillPermissionError("Trusted function capability relay did not become ready")
        except Exception:
            self._stop_function_capability_relay(network_name, relay_name)
            raise

    def _stop_function_capability_relay(self, network_name: str, relay_name: str) -> None:
        for command in (
            ["docker", "rm", "-f", relay_name],
            ["docker", "network", "rm", network_name],
        ):
            try:
                self.docker_runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    shell=False,
                )
            except Exception:
                pass

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
        run.error_message = _merge_error_message(run.error_message, error_message)
        run.ended_at = utc_now()
        self.db.commit()


SkillRunner = LocalSkillRunner
