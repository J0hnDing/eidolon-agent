import hashlib
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Skill, SkillRun, WebAppAuditRecord, WebAppInstance, WebAppSession
from app.schemas.manifest import SkillManifest
from app.schemas.web_app import WebAppContainmentPolicy, WebAppOpenResponse, WebAppSessionRead
from app.services.docker_image_manager import DockerImageManager
from app.services.manifest_validator import validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_runner import RunnerConfig, is_docker_available, validate_supported_permissions

ACTIVE_INSTANCE_STATUSES = {"starting", "ready", "healthy", "unhealthy"}
DEFAULT_INTERNAL_NETWORK = "personal-agent-web-app-internal"
DEFAULT_BACKEND_ORIGIN = "http://localhost:8000"
DEFAULT_GATEWAY_DOMAIN = "web-app.localhost"
HEALTH_PATH = "/__personal_agent__/health"
_LOCAL_PROCESSES: dict[str, subprocess.Popen[bytes]] = {}


class WebAppRuntimeError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class WebAppRuntimeConfig:
    mode: str
    docker_image: str
    memory_limit: str
    cpu_limit: str
    runtime_root: Path
    readiness_timeout_seconds: float = 15
    idle_timeout_seconds: int = 900
    session_ttl_seconds: int = 43_200
    maintenance_interval_seconds: int = 30
    backend_origin: str = DEFAULT_BACKEND_ORIGIN
    gateway_domain: str = DEFAULT_GATEWAY_DOMAIN
    internal_network: str = DEFAULT_INTERNAL_NETWORK
    egress_dns: str = "1.1.1.1"
    max_request_bytes: int = 1_000_000
    max_response_bytes: int = 5_000_000
    max_audit_records_per_instance: int = 200

    @classmethod
    def from_env(cls) -> "WebAppRuntimeConfig":
        runner = RunnerConfig.from_env()
        return cls(
            mode=runner.mode,
            docker_image=runner.docker_image,
            memory_limit=runner.memory_limit,
            cpu_limit=runner.cpu_limit,
            runtime_root=Path(
                os.getenv(
                    "PERSONAL_AGENT_WEB_APP_RUNTIME_ROOT",
                    str(Path(__file__).resolve().parents[3] / "runtime" / "web_apps"),
                )
            ),
            readiness_timeout_seconds=float(os.getenv("PERSONAL_AGENT_WEB_APP_READINESS_SECONDS", "15")),
            idle_timeout_seconds=int(os.getenv("PERSONAL_AGENT_WEB_APP_IDLE_SECONDS", "900")),
            session_ttl_seconds=int(os.getenv("PERSONAL_AGENT_WEB_APP_SESSION_SECONDS", "43200")),
            maintenance_interval_seconds=int(os.getenv("PERSONAL_AGENT_WEB_APP_MAINTENANCE_SECONDS", "30")),
            backend_origin=os.getenv("PERSONAL_AGENT_BACKEND_ORIGIN", DEFAULT_BACKEND_ORIGIN).rstrip("/"),
            gateway_domain=os.getenv("PERSONAL_AGENT_WEB_APP_GATEWAY_DOMAIN", DEFAULT_GATEWAY_DOMAIN).strip("."),
            internal_network=os.getenv("PERSONAL_AGENT_WEB_APP_INTERNAL_NETWORK", DEFAULT_INTERNAL_NETWORK),
            egress_dns=os.getenv("PERSONAL_AGENT_WEB_APP_EGRESS_DNS", "1.1.1.1"),
        )

    @property
    def gateway_scheme(self) -> str:
        return urlsplit(self.backend_origin).scheme or "http"

    @property
    def gateway_port(self) -> int:
        parsed = urlsplit(self.backend_origin)
        return parsed.port or (443 if parsed.scheme == "https" else 80)


@dataclass(frozen=True)
class LaunchResult:
    runner_mode: str
    upstream_url: str
    container_id: str | None = None
    relay_container_id: str | None = None
    process_id: int | None = None


class WebAppLauncher(Protocol):
    def start(
        self,
        *,
        instance_id: str,
        skill_id: int,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest,
        capability_token: str,
    ) -> LaunchResult: ...

    def stop(self, instance: WebAppInstance) -> str: ...

    def logs(self, instance: WebAppInstance) -> str: ...


class PersistentWebAppLauncher:
    def __init__(
        self,
        config: WebAppRuntimeConfig,
        *,
        docker_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        docker_available_checker: Callable[[], bool] = is_docker_available,
        image_manager: DockerImageManager | None = None,
    ) -> None:
        self.config = config
        self.docker_runner = docker_runner
        self.docker_available_checker = docker_available_checker
        self.image_manager = image_manager or DockerImageManager(config.docker_image, docker_runner=docker_runner)

    def start(
        self,
        *,
        instance_id: str,
        skill_id: int,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest,
        capability_token: str,
    ) -> LaunchResult:
        if self.config.mode in {"local", "dev"}:
            return self._start_local(
                instance_id=instance_id,
                skill_id=skill_id,
                skill_dir=skill_dir,
                cache_dir=cache_dir,
                manifest=manifest,
                capability_token=capability_token,
            )
        if self.config.mode not in {"auto", "docker"}:
            raise WebAppRuntimeError("Unknown runner mode. Use auto, docker, local, or dev.")
        return self._start_docker(
            instance_id=instance_id,
            skill_id=skill_id,
            skill_dir=skill_dir,
            cache_dir=cache_dir,
            manifest=manifest,
            capability_token=capability_token,
        )

    def _start_docker(
        self,
        *,
        instance_id: str,
        skill_id: int,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest,
        capability_token: str,
    ) -> LaunchResult:
        if not self.docker_available_checker():
            raise WebAppRuntimeError(
                "Docker sandbox runner is selected, but Docker is unavailable. "
                "Set PERSONAL_AGENT_RUNNER_MODE=local only for explicit development fallback."
            )
        self.image_manager.ensure_image()
        has_network_egress = bool(manifest.permissions.network)
        self._ensure_internal_network()
        network = self.config.internal_network

        container_name = f"personal-agent-web-{instance_id[:12]}"
        relay_container_name = f"personal-agent-web-relay-{instance_id[:12]}"
        backend_url = f"http://{relay_container_name}:8001"
        cache_read = self._has_cache_permission(manifest.permissions.filesystem_read)
        cache_write = self._has_cache_permission(manifest.permissions.filesystem_write)
        cache_mount = (
            ["-v", f"{cache_dir.resolve()}:/skill/cache:{'rw' if cache_write else 'ro'}"]
            if cache_read or cache_write
            else []
        )
        command = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "--network",
            network,
            "--memory",
            self.config.memory_limit,
            "--cpus",
            self.config.cpu_limit,
            "--pids-limit",
            "128",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--add-host",
            "host.docker.internal:127.0.0.1",
            "--add-host",
            "gateway.docker.internal:127.0.0.1",
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
            "graph.microsoft.com:127.0.0.1",
            "--add-host",
            "login.microsoftonline.com:127.0.0.1",
            "--add-host",
            "api.telegram.org:127.0.0.1",
            *(["--dns", self.config.egress_dns] if has_network_egress else []),
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "PYTHONPATH=/package:/package/.deps:/runtime",
            "-e",
            f"PERSONAL_AGENT_SKILL_ID={skill_id}",
            "-e",
            f"PERSONAL_AGENT_WEB_INSTANCE_ID={instance_id}",
            "-e",
            f"PERSONAL_AGENT_WEB_INSTANCE_TOKEN={capability_token}",
            "-e",
            f"PERSONAL_AGENT_BACKEND_URL={backend_url}",
            "-e",
            f"PERSONAL_AGENT_WEB_ENTRYPOINT={manifest.entrypoint}",
            "-e",
            "PERSONAL_AGENT_SKILL_CACHE_DIR=/skill/cache",
            "-v",
            f"{skill_dir.resolve()}:/package:ro",
            *cache_mount,
            self.config.docker_image,
            "python",
            "-m",
            "uvicorn",
            "web_runtime_host:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--no-access-log",
        ]
        result = self.docker_runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if result.returncode != 0:
            raise WebAppRuntimeError(result.stderr.strip() or "Docker web application startup failed")
        container_id = result.stdout.strip() or container_name
        relay_container_id = None
        ingress_container_id = container_id
        relay_result = self.docker_runner(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                relay_container_name,
                "--network",
                "bridge",
                "--memory",
                "64m",
                "--cpus",
                "0.25",
                "--pids-limit",
                "64",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--add-host",
                "host.docker.internal:host-gateway",
                "-p",
                "127.0.0.1::8000",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                f"PERSONAL_AGENT_RELAY_APP_HOST={container_name}",
                "-e",
                "PERSONAL_AGENT_RELAY_APP_PORT=8000",
                "-e",
                f"PERSONAL_AGENT_RELAY_BACKEND_URL={self._docker_backend_url()}",
                self.config.docker_image,
                "python",
                "/runtime/web_runtime_relay.py",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if relay_result.returncode != 0:
            self._remove_docker_container(container_id)
            raise WebAppRuntimeError(relay_result.stderr.strip() or "Private web application relay startup failed")
        relay_container_id = relay_result.stdout.strip() or relay_container_name
        connect_result = self.docker_runner(
            ["docker", "network", "connect", self.config.internal_network, relay_container_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if connect_result.returncode != 0:
            self._remove_docker_container(relay_container_id)
            self._remove_docker_container(container_id)
            raise WebAppRuntimeError(
                connect_result.stderr.strip() or "Could not connect the private web application ingress relay"
            )
        if has_network_egress:
            egress_result = self.docker_runner(
                ["docker", "network", "connect", "--gw-priority", "1", "bridge", container_id],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            if egress_result.returncode != 0:
                self._remove_docker_container(relay_container_id)
                self._remove_docker_container(container_id)
                raise WebAppRuntimeError(
                    egress_result.stderr.strip() or "Could not enable approved web application server egress"
                )
        ingress_container_id = relay_container_id
        port_result = self.docker_runner(
            ["docker", "port", ingress_container_id, "8000/tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if port_result.returncode != 0:
            if relay_container_id:
                self._remove_docker_container(relay_container_id)
            self._remove_docker_container(container_id)
            raise WebAppRuntimeError(port_result.stderr.strip() or "Could not resolve web application ingress port")
        try:
            port = self._parse_published_port(port_result.stdout)
        except WebAppRuntimeError:
            if relay_container_id:
                self._remove_docker_container(relay_container_id)
            self._remove_docker_container(container_id)
            raise
        return LaunchResult(
            runner_mode="docker",
            upstream_url=f"http://127.0.0.1:{port}",
            container_id=container_id,
            relay_container_id=relay_container_id,
        )

    def _start_local(
        self,
        *,
        instance_id: str,
        skill_id: int,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest,
        capability_token: str,
    ) -> LaunchResult:
        port = self._free_loopback_port()
        instance_dir = self.config.runtime_root / instance_id
        instance_dir.mkdir(parents=True, exist_ok=True)
        stdout_handle = (instance_dir / "stdout.log").open("ab")
        stderr_handle = (instance_dir / "stderr.log").open("ab")
        env = self._local_env(
            instance_id=instance_id,
            skill_id=skill_id,
            skill_dir=skill_dir,
            cache_dir=cache_dir,
            manifest=manifest,
            capability_token=capability_token,
        )
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "web_runtime_host:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-access-log",
                ],
                # Keep relative ./cache state in the same controlled per-skill
                # location used by Docker. Source stays on PYTHONPATH and is not
                # used as the process working directory.
                cwd=cache_dir.parent,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        _LOCAL_PROCESSES[instance_id] = process
        return LaunchResult(
            runner_mode="local_dev",
            upstream_url=f"http://127.0.0.1:{port}",
            process_id=process.pid,
        )

    def stop(self, instance: WebAppInstance) -> str:
        logs = self.logs(instance)
        if instance.runner_mode == "docker" and instance.container_id:
            failures = []
            for container_id in (instance.relay_container_id, instance.container_id):
                if not container_id:
                    continue
                result = self._remove_docker_container(container_id)
                if result.returncode != 0 and "no such container" not in result.stderr.lower():
                    failures.append(result.stderr.strip() or f"Could not remove Docker container {container_id}")
            if failures:
                raise WebAppRuntimeError("; ".join(failures))
        elif instance.process_id:
            process = _LOCAL_PROCESSES.pop(instance.id, None)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            elif process is None:
                try:
                    os.kill(instance.process_id, signal.SIGTERM)
                except OSError:
                    pass
        return logs

    def logs(self, instance: WebAppInstance) -> str:
        if instance.runner_mode == "docker" and instance.container_id:
            parts = []
            for label, container_id in (
                ("application", instance.container_id),
                ("private ingress relay", instance.relay_container_id),
            ):
                if not container_id:
                    continue
                result = self.docker_runner(
                    ["docker", "logs", "--tail", "200", container_id],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                )
                output = "\n".join(part for part in (result.stdout, result.stderr) if part)
                if output:
                    parts.append(f"[{label}]\n{output}")
            return "\n".join(parts)[-16_000:]
        instance_dir = self.config.runtime_root / instance.id
        parts = []
        for name in ("stdout.log", "stderr.log"):
            path = instance_dir / name
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)[-16_000:]

    def _remove_docker_container(self, container_id: str) -> subprocess.CompletedProcess[str]:
        return self.docker_runner(
            ["docker", "rm", "-f", container_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )

    def _ensure_internal_network(self) -> None:
        inspect_result = self.docker_runner(
            ["docker", "network", "inspect", self.config.internal_network],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if inspect_result.returncode == 0:
            return
        create_result = self.docker_runner(
            ["docker", "network", "create", "--internal", self.config.internal_network],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if create_result.returncode != 0:
            raise WebAppRuntimeError(create_result.stderr.strip() or "Could not create private web application network")

    def _docker_backend_url(self) -> str:
        return os.getenv("PERSONAL_AGENT_DOCKER_BACKEND_URL", "http://host.docker.internal:8000")

    @staticmethod
    def _has_cache_permission(paths: list[str]) -> bool:
        return any(path.replace("\\", "/").removeprefix("./").rstrip("/") == "cache" for path in paths)

    def _local_env(
        self,
        *,
        instance_id: str,
        skill_id: int,
        skill_dir: Path,
        cache_dir: Path,
        manifest: SkillManifest,
        capability_token: str,
    ) -> dict[str, str]:
        allowed_host_names = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed_host_names}
        backend_dir = Path(__file__).resolve().parents[2]
        python_paths = [str(skill_dir.resolve()), str((skill_dir / ".deps").resolve()), str(backend_dir)]
        env.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(python_paths),
                "PERSONAL_AGENT_SKILL_ID": str(skill_id),
                "PERSONAL_AGENT_WEB_INSTANCE_ID": instance_id,
                "PERSONAL_AGENT_WEB_INSTANCE_TOKEN": capability_token,
                "PERSONAL_AGENT_BACKEND_URL": self.config.backend_origin,
                "PERSONAL_AGENT_WEB_ENTRYPOINT": manifest.entrypoint,
                "PERSONAL_AGENT_SKILL_CACHE_DIR": str(cache_dir.resolve()),
            }
        )
        return env

    @staticmethod
    def _parse_published_port(output: str) -> int:
        first = next((line.strip() for line in output.splitlines() if line.strip()), "")
        value = first.rsplit(":", 1)[-1]
        if not value.isdigit():
            raise WebAppRuntimeError("Docker returned an unreadable web application ingress port")
        return int(value)

    @staticmethod
    def _free_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def default_health_checker(upstream_url: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(
                Request(f"{upstream_url}{HEALTH_PATH}", headers={"User-Agent": "personal-agent-readiness"}),
                timeout=min(1.0, max(0.1, deadline - time.monotonic())),
            ) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError):
            time.sleep(0.1)
    return False


@dataclass
class WebAppRuntimeService:
    db: Session
    project_root: Path | None = None
    config: WebAppRuntimeConfig | None = None
    launcher: WebAppLauncher | None = None
    health_checker: Callable[[str, float], bool] = default_health_checker

    def __post_init__(self) -> None:
        if self.project_root is None:
            self.project_root = Path(__file__).resolve().parents[3]
        self.project_root = self.project_root.resolve()
        self.config = self.config or WebAppRuntimeConfig.from_env()
        self.launcher = self.launcher or PersistentWebAppLauncher(self.config)
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)

    def open_session(self, skill: Skill) -> WebAppOpenResponse:
        manifest, skill_dir = self._open_preconditions(skill)
        instance = self._healthy_instance(skill)
        if instance is not None and not self._instance_responds(instance):
            instance = None
        if instance is None:
            try:
                with SkillOperationGuard(self.db).locked(skill, "web_app_start", reason="Starting web application"):
                    instance = self._healthy_instance(skill)
                    if instance is not None and not self._instance_responds(instance):
                        self.stop_instance(instance, "Health check failed before session open", failed=True)
                        instance = None
                    if instance is None:
                        for stale_instance in self._active_instances(skill):
                            self.stop_instance(
                                stale_instance,
                                "Replaced non-healthy or stale-version application instance",
                                failed=True,
                            )
                    instance = instance or self._start_instance(skill, skill_dir, manifest)
            except SkillOperationConflict as exc:
                raise WebAppRuntimeError(str(exc)) from exc
        session_token = secrets.token_hex(16)
        session_id = uuid4().hex
        gateway_base_host = f"w{instance.id[:8]}-{session_id[:16]}.{self.config.gateway_domain}"
        gateway_host = self._session_host(gateway_base_host, session_token)
        now = utc_now()
        session = WebAppSession(
            id=session_id,
            instance_id=instance.id,
            skill_id=skill.id,
            status="active",
            token_hash=token_hash(session_token),
            # Persist the non-secret origin identity. The bearer component is
            # returned once in the embedding URL and stored only as a hash.
            gateway_host=gateway_base_host,
            created_at=now,
            last_accessed_at=now,
            expires_at=now + timedelta(seconds=self.config.session_ttl_seconds),
        )
        instance.last_accessed_at = now
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        self.db.refresh(instance)
        self.record_audit(instance, "session_open", "succeeded", session=session)
        return WebAppOpenResponse(
            instance=instance,
            session=WebAppSessionRead.model_validate(session).model_copy(update={"gateway_host": gateway_host}),
            embed_url=self._embed_url(gateway_host),
            containment=self.containment_policy(instance.runner_mode, manifest),
        )

    def resolve_gateway_session(self, host: str) -> tuple[WebAppSession, WebAppInstance, Skill]:
        hostname = host.partition(":")[0].lower().rstrip(".")
        suffix = f".{self.config.gateway_domain.lower()}"
        if not hostname.endswith(suffix):
            raise WebAppRuntimeError("Web application gateway requires its isolated origin")
        label = hostname[: -len(suffix)]
        base_label, separator, session_token = label.rpartition("-")
        if separator != "-" or not base_label or not session_token:
            raise WebAppRuntimeError("Web application session is invalid")
        session = self.db.scalar(select(WebAppSession).where(WebAppSession.token_hash == token_hash(session_token)))
        expected_base_host = f"{base_label}{suffix}"
        if session is None or session.gateway_host.lower() != expected_base_host:
            raise WebAppRuntimeError("Web application session is invalid")
        now = utc_now()
        if session.status != "active" or _aware(session.expires_at) <= now:
            if session.status == "active":
                session.status = "expired"
                session.closed_at = now
                self.db.commit()
            raise WebAppRuntimeError("Web application session has expired")
        instance = self.db.get(WebAppInstance, session.instance_id)
        skill = self.db.get(Skill, session.skill_id)
        if instance is None or skill is None:
            raise WebAppRuntimeError("Web application session target no longer exists")
        if (
            instance.status != "healthy"
            or skill.status != "installed"
            or not skill.enabled
            or skill.runtime != "web_app"
            or skill.active_version_id != instance.version_id
        ):
            raise WebAppRuntimeError("Web application session is no longer active")
        session.last_accessed_at = now
        instance.last_accessed_at = now
        self.db.commit()
        return session, instance, skill

    def instance_for_capability(self, bearer_token: str) -> tuple[WebAppInstance, Skill, SkillManifest]:
        instance = self.db.scalar(
            select(WebAppInstance).where(WebAppInstance.capability_token_hash == token_hash(bearer_token))
        )
        if instance is None or instance.status != "healthy":
            raise WebAppRuntimeError("Web application capability is invalid or inactive")
        skill = self.db.get(Skill, instance.skill_id)
        if (
            skill is None
            or skill.status != "installed"
            or not skill.enabled
            or skill.runtime != "web_app"
            or skill.active_version_id != instance.version_id
        ):
            raise WebAppRuntimeError("Web application capability is no longer authorized")
        manifest = validate_manifest_file(self.proposed_service.skill_dir_for_record(skill) / "manifest.json")
        return instance, skill, manifest

    def mark_unhealthy(self, instance: WebAppInstance, message: str) -> None:
        if instance.status in ACTIVE_INSTANCE_STATUSES:
            now = utc_now()
            instance.status = "unhealthy"
            instance.error_message = message[:2000]
            sessions = self.db.scalars(
                select(WebAppSession)
                .where(WebAppSession.instance_id == instance.id)
                .where(WebAppSession.status == "active")
            ).all()
            for session in sessions:
                session.status = "closed"
                session.closed_at = now
            self.db.commit()

    def stop_skill_instances(self, skill: Skill, reason: str) -> None:
        instances = list(
            self.db.scalars(
                select(WebAppInstance)
                .where(WebAppInstance.skill_id == skill.id)
                .where(WebAppInstance.status.in_(ACTIVE_INSTANCE_STATUSES))
            ).all()
        )
        for instance in instances:
            self.stop_instance(instance, reason)

    def stop_instance(self, instance: WebAppInstance, reason: str, *, failed: bool = False) -> None:
        if instance.status not in ACTIVE_INSTANCE_STATUSES:
            return
        try:
            instance.logs = self.launcher.stop(instance)[-16_000:]
        except Exception as exc:
            instance.logs = self._safe_launcher_logs(instance)
            reason = f"{reason}; runtime cleanup reported: {exc}"
            failed = True
        now = utc_now()
        instance.status = "failed" if failed else "stopped"
        instance.error_message = reason[:2000] if failed else None
        instance.stopped_at = now
        sessions = self.db.scalars(
            select(WebAppSession).where(WebAppSession.instance_id == instance.id).where(WebAppSession.status == "active")
        ).all()
        for session in sessions:
            session.status = "closed"
            session.closed_at = now
        self.db.commit()
        self.record_audit(
            instance,
            "instance_stop",
            "failed" if failed else "succeeded",
            request={"reason": reason[:500]},
            error_message=reason if failed else None,
        )

    def stop_idle_instances(self) -> int:
        cutoff = utc_now() - timedelta(seconds=self.config.idle_timeout_seconds)
        instances = list(
            self.db.scalars(
                select(WebAppInstance).where(WebAppInstance.status.in_(ACTIVE_INSTANCE_STATUSES))
            ).all()
        )
        stopped = 0
        for instance in instances:
            last_access = instance.last_accessed_at or instance.ready_at or instance.started_at or instance.created_at
            if _aware(last_access) <= cutoff:
                self.stop_instance(instance, "Idle timeout reached")
                stopped += 1
        return stopped

    def recover_stale_instances(self) -> int:
        instances = list(
            self.db.scalars(
                select(WebAppInstance).where(WebAppInstance.status.in_(ACTIVE_INSTANCE_STATUSES))
            ).all()
        )
        for instance in instances:
            self.stop_instance(instance, "Recovered stale instance after backend restart")
        return len(instances)

    def shutdown_all(self) -> int:
        instances = list(
            self.db.scalars(
                select(WebAppInstance).where(WebAppInstance.status.in_(ACTIVE_INSTANCE_STATUSES))
            ).all()
        )
        for instance in instances:
            self.stop_instance(instance, "Backend shutdown")
        return len(instances)

    def delete_skill_runtime_records(self, skill: Skill) -> None:
        self.stop_skill_instances(skill, "Skill deleted")
        instance_ids = list(
            self.db.scalars(select(WebAppInstance.id).where(WebAppInstance.skill_id == skill.id)).all()
        )
        if instance_ids:
            self.db.query(SkillRun).filter(SkillRun.web_app_instance_id.in_(instance_ids)).update(
                {SkillRun.web_app_instance_id: None},
                synchronize_session=False,
            )
            self.db.execute(delete(WebAppAuditRecord).where(WebAppAuditRecord.instance_id.in_(instance_ids)))
            self.db.execute(delete(WebAppSession).where(WebAppSession.instance_id.in_(instance_ids)))
            self.db.execute(delete(WebAppInstance).where(WebAppInstance.id.in_(instance_ids)))
            self.db.commit()

    def record_audit(
        self,
        instance: WebAppInstance,
        operation: str,
        status: str,
        *,
        session: WebAppSession | None = None,
        request: dict | None = None,
        response: dict | None = None,
        error_message: str | None = None,
    ) -> WebAppAuditRecord:
        record = WebAppAuditRecord(
            instance_id=instance.id,
            session_id=session.id if session else None,
            operation=operation,
            status=status,
            request_json=request or {},
            response_json=response or {},
            error_message=error_message[:2000] if error_message else None,
            started_at=utc_now(),
            ended_at=utc_now(),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        ids = list(
            self.db.scalars(
                select(WebAppAuditRecord.id)
                .where(WebAppAuditRecord.instance_id == instance.id)
                .order_by(WebAppAuditRecord.id.desc())
                .offset(self.config.max_audit_records_per_instance)
            ).all()
        )
        if ids:
            self.db.execute(delete(WebAppAuditRecord).where(WebAppAuditRecord.id.in_(ids)))
            self.db.commit()
        return record

    def containment_policy(self, runner_mode: str, manifest: SkillManifest) -> WebAppContainmentPolicy:
        server_network = (
            "Approved domains enable container egress, but domain-level filtering is not yet enforced."
            if manifest.permissions.network
            else (
                "The skill container has no external route; a separate trusted relay provides loopback ingress "
                "and only the scoped Codex-capability backend path."
            )
        )
        if runner_mode != "docker":
            server_network = "Explicit local development fallback is less isolated and does not enforce Docker networking."
        return WebAppContainmentPolicy(
            iframe_sandbox="allow-scripts allow-forms allow-same-origin allow-modals",
            content_security_policy=(
                "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors http://localhost:* "
                "http://127.0.0.1:*; form-action 'self'; navigate-to 'self'; connect-src 'self'; "
                "img-src 'self' data: blob:; "
                "font-src 'self' data:; media-src 'self' blob:; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; worker-src 'self' blob:"
            ),
            permissions_policy=(
                "accelerometer=(), autoplay=(), camera=(), clipboard-read=(), clipboard-write=(), "
                "display-capture=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
                "payment=(), publickey-credentials-get=(), usb=()"
            ),
            browser_network="Browser requests are restricted to the session's isolated gateway origin.",
            origin_isolation="Every application session receives a unique untrusted *.web-app.localhost origin.",
            websocket_support="WebSockets are intentionally deferred for Milestone 1.",
            runner_isolation=(
                "Docker sandbox with read-only package, controlled cache mount, resource limits, and dropped capabilities."
                if runner_mode == "docker"
                else "Explicit local development fallback; less isolated than Docker."
            ),
            server_network_enforcement=server_network,
        )

    def _open_preconditions(self, skill: Skill) -> tuple[SkillManifest, Path]:
        if skill.status != "installed":
            raise WebAppRuntimeError("Only installed web application skills can be opened")
        if not skill.enabled:
            raise WebAppRuntimeError("Web application skill is disabled")
        if skill.runtime != "web_app":
            raise WebAppRuntimeError("Only web_app runtime skills can be opened as applications")
        if skill.active_version_id is None:
            raise WebAppRuntimeError("Web application skill does not have an active version")
        permission = PermissionService(self.db, project_root=self.project_root).can_run(skill)
        if not permission.allowed:
            raise WebAppRuntimeError(permission.reason)
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        manifest = validate_manifest_file(skill_dir / "manifest.json")
        if manifest.runtime != "web_app":
            raise WebAppRuntimeError("Active manifest runtime does not match the web application record")
        validate_supported_permissions(manifest)
        return manifest, skill_dir

    def _healthy_instance(self, skill: Skill) -> WebAppInstance | None:
        if skill.active_version_id is None:
            return None
        return self.db.scalar(
            select(WebAppInstance)
            .where(WebAppInstance.skill_id == skill.id)
            .where(WebAppInstance.version_id == skill.active_version_id)
            .where(WebAppInstance.status == "healthy")
            .order_by(WebAppInstance.created_at.desc())
        )

    def _active_instances(self, skill: Skill) -> list[WebAppInstance]:
        return list(
            self.db.scalars(
                select(WebAppInstance)
                .where(WebAppInstance.skill_id == skill.id)
                .where(WebAppInstance.status.in_(ACTIVE_INSTANCE_STATUSES))
                .order_by(WebAppInstance.created_at.desc())
            ).all()
        )

    def _instance_responds(self, instance: WebAppInstance) -> bool:
        if not instance.upstream_url:
            return False
        return self.health_checker(
            instance.upstream_url,
            min(2.0, self.config.readiness_timeout_seconds),
        )

    def _start_instance(self, skill: Skill, skill_dir: Path, manifest: SkillManifest) -> WebAppInstance:
        now = utc_now()
        capability_token = secrets.token_urlsafe(32)
        instance = WebAppInstance(
            id=uuid4().hex,
            skill_id=skill.id,
            version_id=skill.active_version_id,
            status="starting",
            runner_mode="docker" if self.config.mode not in {"local", "dev"} else "local_dev",
            capability_token_hash=token_hash(capability_token),
            created_at=now,
            started_at=now,
            last_accessed_at=now,
        )
        self.db.add(instance)
        self.db.commit()
        self.db.refresh(instance)
        self.record_audit(instance, "instance_start", "running")
        cache_dir = self._cache_dir(skill.id)
        try:
            launch = self.launcher.start(
                instance_id=instance.id,
                skill_id=skill.id,
                skill_dir=skill_dir,
                cache_dir=cache_dir,
                manifest=manifest,
                capability_token=capability_token,
            )
            instance.runner_mode = launch.runner_mode
            instance.upstream_url = launch.upstream_url
            instance.container_id = launch.container_id
            instance.relay_container_id = launch.relay_container_id
            instance.process_id = launch.process_id
            instance.status = "ready"
            self.db.commit()
            if not self.health_checker(launch.upstream_url, self.config.readiness_timeout_seconds):
                raise WebAppRuntimeError(
                    f"Web application did not become ready within {self.config.readiness_timeout_seconds:g} seconds"
                )
            instance.status = "healthy"
            instance.ready_at = utc_now()
            instance.last_accessed_at = instance.ready_at
            self.db.commit()
            self.db.refresh(instance)
            self.record_audit(instance, "readiness", "succeeded")
            return instance
        except Exception as exc:
            instance.error_message = str(exc)[:2000]
            try:
                instance.logs = self.launcher.stop(instance)[-16_000:]
            except Exception:
                instance.logs = self._safe_launcher_logs(instance)
            instance.status = "failed"
            instance.stopped_at = utc_now()
            self.db.commit()
            self.record_audit(instance, "readiness", "failed", error_message=str(exc))
            raise WebAppRuntimeError(str(exc)) from exc

    def _safe_launcher_logs(self, instance: WebAppInstance) -> str:
        try:
            return self.launcher.logs(instance)[-16_000:]
        except Exception as exc:
            return f"Runtime logs could not be captured: {exc}"[:16_000]

    def _cache_dir(self, skill_id: int) -> Path:
        cache_dir = (self.project_root / "runtime" / "skill_cache" / f"skill_{skill_id}" / "cache").resolve()
        runtime_root = (self.project_root / "runtime" / "skill_cache").resolve()
        if not cache_dir.is_relative_to(runtime_root):
            raise WebAppRuntimeError("Web application cache path escaped the controlled runtime root")
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _embed_url(self, gateway_host: str) -> str:
        default_port = 443 if self.config.gateway_scheme == "https" else 80
        port = "" if self.config.gateway_port == default_port else f":{self.config.gateway_port}"
        return f"{self.config.gateway_scheme}://{gateway_host}{port}/"

    def _session_host(self, gateway_base_host: str, session_token: str) -> str:
        label, separator, domain = gateway_base_host.partition(".")
        if not separator:
            raise WebAppRuntimeError("Web application gateway domain is invalid")
        return f"{label}-{session_token}.{domain}"
