from __future__ import annotations

import json
import logging
import os
import posixpath
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import QuercusCourse, QuercusProcessingSetting, QuercusSyncResource
from app.services.act_workspace_service import ensure_act_workspace, refresh_act_agent_instructions
from app.services.quercus_runtime import QUERCUS_WORK_LOCK

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
QUERCUS_PROCESSING_ROOT = PROJECT_ROOT / "runtime" / "backend" / "quercus-processing"
PROCESSING_TEMPORARY_PREFIXES = ("marker-", "llama-start-")

PROCESSING_NONE = "none"
PROCESSING_MARKER = "marker_surya_llamacpp"
PROCESSING_METHODS = {PROCESSING_NONE, PROCESSING_MARKER}
INFERENCE_URL = "http://127.0.0.1:8081/v1"
FAILURE_PLACEHOLDER = "Processing failed, refer to the raw file.\n"
PROCESS_TIMEOUT_SECONDS = 60 * 60
INFERENCE_START_TIMEOUT_SECONDS = 15 * 60
SUPPORTED_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".pptx",
    ".docx",
    ".xlsx",
    ".html",
    ".htm",
    ".epub",
}


class ProcessingInterrupted(RuntimeError):
    pass


class InferenceStartupError(RuntimeError):
    pass


_LLAMA_BOOTSTRAP_CODE = """
import json
import os
import sys
from pathlib import Path

from surya.inference.backends.llamacpp import LlamaCppBackend

handle = LlamaCppBackend().start()
sentinel_path = Path.home() / ".cache" / "datalab" / "surya" / "llamacpp_server.json"
sentinel = json.loads(sentinel_path.read_text(encoding="utf-8"))
if handle.spawned_by_us:
    sentinel["eidolon_owner_pid"] = os.getppid()
    sentinel_path.write_text(json.dumps(sentinel), encoding="utf-8")
Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "base_url": handle.base_url,
            "pid": sentinel.get("pid"),
            "port": sentinel.get("port"),
            "spawned_by_us": handle.spawned_by_us,
            "eidolon_owner_pid": sentinel.get("eidolon_owner_pid"),
        }
    ),
    encoding="utf-8",
)
""".strip()


class LlamaCppServerManager:
    """Own one Surya llama.cpp server for a backend processing pass."""

    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.stop_event = stop_event
        self._url: str | None = None
        self._owned_pid: int | None = None
        self._start_error: InferenceStartupError | None = None

    def ensure_running(
        self,
        *,
        marker_executable: str,
        llama_server: Path,
        temporary_root: Path,
    ) -> str:
        if self._url is not None and self._probe_health(self._url):
            return self._url
        if self._owned_pid is not None:
            stale_pid = self._owned_pid
            self._owned_pid = None
            self._terminate_pid_tree(stale_pid)
            self._delete_matching_sentinel(stale_pid)
            self._url = None
        if self._start_error is not None:
            raise self._start_error
        self._raise_if_stopping()
        temporary_root.mkdir(parents=True, exist_ok=True)
        python_executable = self._marker_python(marker_executable)
        environment = self._server_environment(llama_server)
        started_at = time.monotonic()
        try:
            with TemporaryDirectory(
                dir=temporary_root,
                prefix="llama-start-",
                ignore_cleanup_errors=True,
            ) as output_dir:
                output_root = Path(output_dir)
                descriptor_path = output_root / "server.json"
                log_path = output_root / "server-startup.log"
                arguments = [python_executable, "-c", _LLAMA_BOOTSTRAP_CODE, str(descriptor_path)]
                completed = self._run_startup(arguments, environment, output_root, log_path)
                if completed.returncode != 0:
                    raise InferenceStartupError(
                        f"llama.cpp startup failed: {self._read_log_tail(log_path)}"
                    )
                descriptor = self._read_descriptor(descriptor_path)
                inference_url = self._validate_url(descriptor.get("base_url"))
                if not self._probe_health(inference_url):
                    raise InferenceStartupError("llama.cpp did not remain healthy after startup")
                pid = self._validate_pid(descriptor.get("pid"))
                spawned = descriptor.get("spawned_by_us") is True
                previously_owned = isinstance(descriptor.get("eidolon_owner_pid"), int)
                self._url = inference_url
                if spawned or previously_owned:
                    self._owned_pid = pid
                    self._mark_sentinel_owned(pid)
                logger.info(
                    "Quercus inference server ready url=%s pid=%s owned=%s duration_seconds=%.1f",
                    inference_url,
                    pid,
                    spawned or previously_owned,
                    time.monotonic() - started_at,
                )
                return inference_url
        except ProcessingInterrupted:
            self._cleanup_unhealthy_sentinel()
            raise
        except InferenceStartupError as exc:
            self._start_error = exc
            logger.error("Quercus inference server startup failed error=%s", exc)
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = InferenceStartupError("llama.cpp startup produced invalid state")
            self._start_error = error
            logger.exception("Quercus inference server startup failed")
            raise error from exc

    def close(self) -> None:
        pid = self._owned_pid
        self._owned_pid = None
        self._url = None
        self._start_error = None
        if pid is None:
            return
        logger.info("Stopping backend-owned Quercus inference server pid=%s", pid)
        stopped = self._terminate_pid_tree(pid)
        if stopped or not self._sentinel_is_healthy():
            self._delete_matching_sentinel(pid)
        if stopped:
            logger.info("Backend-owned Quercus inference server stopped pid=%s", pid)
        else:
            logger.error("Backend-owned Quercus inference server could not be stopped pid=%s", pid)

    def _run_startup(
        self,
        arguments: list[str],
        environment: dict[str, str],
        output_dir: Path,
        log_path: Path,
    ) -> subprocess.CompletedProcess[Any]:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        started_at = time.monotonic()
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                arguments,
                cwd=output_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            logger.info(
                "Quercus inference bootstrap started pid=%s parent_pid=%s",
                process.pid,
                os.getpid(),
            )
            while process.poll() is None:
                if self.stop_event is not None and self.stop_event.wait(0.25):
                    QuercusProcessingService._terminate_process_tree(
                        process,
                        reason="backend_shutdown_during_inference_startup",
                    )
                    raise ProcessingInterrupted(
                        "Quercus processing interrupted during inference startup"
                    )
                if self.stop_event is None:
                    time.sleep(0.25)
                if time.monotonic() - started_at >= INFERENCE_START_TIMEOUT_SECONDS:
                    QuercusProcessingService._terminate_process_tree(
                        process,
                        reason="inference_startup_timeout",
                    )
                    raise InferenceStartupError("llama.cpp startup timed out")
            logger.info(
                "Quercus inference bootstrap exited pid=%s return_code=%s duration_seconds=%.1f",
                process.pid,
                process.returncode,
                time.monotonic() - started_at,
            )
            return subprocess.CompletedProcess(arguments, process.returncode)

    @staticmethod
    def _server_environment(llama_server: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("SURYA_INFERENCE_URL", None)
        environment.update(
            {
                "SURYA_INFERENCE_BACKEND": "llamacpp",
                "SURYA_INFERENCE_PARALLEL": "1",
                "SURYA_INFERENCE_KEEP_ALIVE": "1",
                "SURYA_INFERENCE_PORT": "8081",
                "LLAMA_CPP_BINARY": str(llama_server),
            }
        )
        environment["PATH"] = os.pathsep.join(
            [str(llama_server.parent), environment.get("PATH", "")]
        )
        return environment

    @staticmethod
    def _marker_python(marker_executable: str) -> str:
        executable = Path(marker_executable).resolve()
        candidate = executable.parent / ("python.exe" if os.name == "nt" else "python")
        if candidate.is_file():
            return str(candidate)
        return sys.executable

    @staticmethod
    def _read_descriptor(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid llama.cpp startup descriptor")
        return value

    @staticmethod
    def _validate_url(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Invalid llama.cpp URL")
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Invalid llama.cpp URL")
        return value.rstrip("/")

    @staticmethod
    def _validate_pid(value: Any) -> int:
        pid = int(value)
        if pid <= 0:
            raise ValueError("Invalid llama.cpp PID")
        return pid

    @staticmethod
    def _probe_health(inference_url: str) -> bool:
        base_url = inference_url.removesuffix("/v1")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def _raise_if_stopping(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise ProcessingInterrupted("Quercus processing interrupted by shutdown")

    @staticmethod
    def _sentinel_path() -> Path:
        return Path.home() / ".cache" / "datalab" / "surya" / "llamacpp_server.json"

    def _mark_sentinel_owned(self, pid: int) -> None:
        path = self._sentinel_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if int(value.get("pid")) != pid:
                return
            value["eidolon_owner_pid"] = os.getpid()
            _atomic_write(path, json.dumps(value).encode("utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Could not mark Quercus inference sentinel as backend-owned")

    def _delete_matching_sentinel(self, pid: int) -> None:
        path = self._sentinel_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if int(value.get("pid")) == pid:
                path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Could not remove Quercus inference sentinel", exc_info=True)

    def _cleanup_unhealthy_sentinel(self) -> None:
        if self._sentinel_is_healthy():
            return
        try:
            self._sentinel_path().unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove unhealthy Quercus inference sentinel")

    def _sentinel_is_healthy(self) -> bool:
        try:
            value = json.loads(self._sentinel_path().read_text(encoding="utf-8"))
            port = int(value["port"])
            return self._probe_health(f"http://127.0.0.1:{port}/v1")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _terminate_pid_tree(pid: int) -> bool:
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=20,
                    check=False,
                )
                return completed.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.25)
        try:
            os.killpg(pid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return True
        except OSError:
            return False

    @staticmethod
    def _read_log_tail(path: Path, *, max_characters: int = 4000) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-max_characters:]
        except OSError:
            return ""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".quercus-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


@dataclass
class QuercusProcessingService:
    db: Session
    executable_resolver: Callable[[str], str | None] = shutil.which
    runner: Callable[..., Any] | None = None
    inference_probe: Callable[[], bool] | None = None
    stop_event: threading.Event | None = None
    server_manager: LlamaCppServerManager | None = None

    def __post_init__(self) -> None:
        self.workspace = ensure_act_workspace()
        if self.server_manager is None:
            self.server_manager = LlamaCppServerManager(self.stop_event)

    def initialize(self) -> str:
        with QUERCUS_WORK_LOCK:
            return self.prepare_locked()

    def prepare_locked(self) -> str:
        method = self.method()
        self._recover_interrupted_locked(method)
        self._cleanup_temporary_output_locked()
        self._migrate_layout_locked()
        refresh_act_agent_instructions(method)
        return method

    def refresh_agent_instructions(self) -> None:
        refresh_act_agent_instructions(self.method())

    def method(self) -> str:
        setting = self._setting()
        return setting.method if setting.method in PROCESSING_METHODS else PROCESSING_NONE

    def _setting(self) -> QuercusProcessingSetting:
        setting = self.db.get(QuercusProcessingSetting, 1)
        if setting is None:
            setting = QuercusProcessingSetting(id=1, method=PROCESSING_NONE)
            self.db.add(setting)
            self.db.commit()
        return setting

    def configure(self, method: str, llama_cpp_directory: str | None) -> None:
        if method not in PROCESSING_METHODS:
            raise ValueError("Unsupported Quercus processing method")
        normalized_directory = self._normalize_llama_cpp_directory(llama_cpp_directory)
        if method == PROCESSING_MARKER and normalized_directory is None:
            raise ValueError(
                "Select a llama.cpp directory containing llama-server.exe before enabling Marker"
            )
        setting = self._setting()
        setting.method = method
        setting.llama_cpp_directory = normalized_directory
        self._update_course_states(method)
        self.db.commit()
        refresh_act_agent_instructions(method)

    def set_method(self, method: str) -> None:
        if method not in PROCESSING_METHODS:
            raise ValueError("Unsupported Quercus processing method")
        setting = self._setting()
        setting.method = method
        self._update_course_states(method)
        self.db.commit()
        refresh_act_agent_instructions(method)

    def _update_course_states(self, method: str) -> None:
        if method == PROCESSING_NONE:
            for course in self.db.scalars(select(QuercusCourse)).all():
                course.last_processing_status = "disabled"
                course.last_processing_error_type = None
        else:
            for course in self.db.scalars(select(QuercusCourse)).all():
                if course.last_processing_status != "running":
                    course.last_processing_status = "pending"

    def status(self) -> dict[str, Any]:
        method = self.method()
        setting = self._setting()
        llama_server = self._llama_server_path(setting.llama_cpp_directory)
        courses = self.db.scalars(select(QuercusCourse)).all()
        statuses = {course.last_processing_status for course in courses}
        if method == PROCESSING_NONE:
            aggregate = "disabled"
        elif "running" in statuses:
            aggregate = "running"
        elif "pending" in statuses:
            aggregate = "pending"
        elif "failed" in statuses:
            aggregate = "failed"
        elif "partial" in statuses:
            aggregate = "partial"
        elif courses:
            aggregate = "succeeded"
        else:
            aggregate = "idle"
        resources = self.db.scalars(
            select(QuercusSyncResource).where(
                QuercusSyncResource.resource_type == "file",
                QuercusSyncResource.download_state == "downloaded",
            )
        ).all()
        inference_available, inference_url = self._inference_state()
        return {
            "method": method,
            "llama_cpp_directory": setting.llama_cpp_directory,
            "llama_cpp_available": llama_server is not None,
            "inference_url": inference_url,
            "marker_available": self.executable_resolver("marker_single") is not None,
            "inference_available": inference_available,
            "status": aggregate,
            "processed_file_count": sum(
                resource.processing_state == "succeeded" for resource in resources
            ),
            "failed_file_count": sum(
                resource.processing_state == "failed" for resource in resources
            ),
        }

    def requeue_failed(self) -> int:
        if self.method() != PROCESSING_MARKER:
            raise ValueError("Enable Marker processing before reprocessing failed files")
        resources = self.db.scalars(
            select(QuercusSyncResource).where(
                QuercusSyncResource.resource_type == "file",
                QuercusSyncResource.download_state == "downloaded",
                QuercusSyncResource.processing_state == "failed",
            )
        ).all()
        course_ids = {resource.course_id for resource in resources}
        for resource in resources:
            resource.processing_state = "pending"
            resource.processing_error_type = None
            resource.processed_at = None
        for course_id in course_ids:
            course = self.db.get(QuercusCourse, course_id)
            if course is None:
                continue
            course.last_processing_status = "pending"
            course.last_processing_error_type = None
            course.failed_processing_count = 0
        self.db.commit()
        logger.info("Queued failed Quercus files for reprocessing count=%s", len(resources))
        return len(resources)

    def run(self) -> dict[str, Any]:
        with QUERCUS_WORK_LOCK:
            try:
                return self._run_locked()
            finally:
                self.close()
                self._cleanup_temporary_output_locked()

    def close(self) -> None:
        if self.server_manager is not None:
            self.server_manager.close()

    def _run_locked(self) -> dict[str, Any]:
        method = self.method()
        if method == PROCESSING_NONE:
            return {"status": "disabled", "processed_file_count": 0, "failed_file_count": 0}
        self._migrate_layout_locked()
        logger.info("Quercus processing pass started")
        total_success = 0
        total_failed = 0
        for course in self.db.scalars(select(QuercusCourse)).all():
            self._raise_if_stopping()
            success, failed = self.process_course_locked(course)
            total_success += success
            total_failed += failed
        status = "partial" if total_success and total_failed else "failed" if total_failed else "succeeded"
        result = {
            "status": status,
            "processed_file_count": total_success,
            "failed_file_count": total_failed,
        }
        logger.info(
            "Quercus processing pass finished status=%s processed=%s failed=%s",
            status,
            total_success,
            total_failed,
        )
        return result

    def process_course_locked(self, course: QuercusCourse) -> tuple[int, int]:
        course_root = self._course_root(course)
        (course_root / "files" / "raw").mkdir(parents=True, exist_ok=True)
        (course_root / "files" / "processed").mkdir(parents=True, exist_ok=True)
        if self.method() == PROCESSING_NONE:
            course.last_processing_status = "disabled"
            self.db.commit()
            return 0, 0

        course.last_processing_started_at = utc_now()
        course.last_processing_status = "running"
        course.last_processing_error_type = None
        self.db.commit()
        executable = self.executable_resolver("marker_single")
        llama_server = self._llama_server_path(self._setting().llama_cpp_directory)
        resources = self.db.scalars(
            select(QuercusSyncResource).where(
                QuercusSyncResource.course_id == course.course_id,
                QuercusSyncResource.resource_type == "file",
                QuercusSyncResource.download_state == "downloaded",
            )
        ).all()
        self._refresh_course_progress(course, resources, status="running")
        logger.info(
            "Quercus course processing started course_id=%s resources=%s",
            course.course_id,
            len(resources),
        )
        for resource in resources:
            self._raise_if_stopping()
            processed_relative = self._processed_path(course, resource)
            raw_path = course_root / PurePosixPath(resource.relative_path or "")
            processed_path = course_root / PurePosixPath(processed_relative)
            reusable = (
                resource.processing_state == "succeeded"
                and resource.processed_source_fingerprint == resource.content_fingerprint
                and processed_path.is_file()
            )
            if reusable:
                continue
            unchanged_failure = (
                resource.processing_state == "failed"
                and resource.processed_source_fingerprint == resource.content_fingerprint
            )
            if unchanged_failure:
                continue
            if (
                resource.processed_source_fingerprint != resource.content_fingerprint
                and processed_path.is_file()
            ):
                processed_path.unlink()
            resource.processing_state = "pending"
            resource.processing_error_type = None
            self._refresh_course_progress(course, resources, status="running")
            try:
                if not raw_path.is_file():
                    self._record_failure(resource, processed_path, "raw_file_missing")
                elif raw_path.suffix.casefold() not in SUPPORTED_SUFFIXES:
                    self._record_failure(resource, processed_path, "unsupported_type")
                elif executable is None:
                    self._record_failure(resource, processed_path, "marker_unavailable")
                elif llama_server is None:
                    self._record_failure(resource, processed_path, "llama_cpp_unavailable")
                else:
                    try:
                        inference_url = self.server_manager.ensure_running(
                            marker_executable=executable,
                            llama_server=llama_server,
                            temporary_root=QUERCUS_PROCESSING_ROOT,
                        )
                    except InferenceStartupError:
                        self._record_failure(
                            resource,
                            processed_path,
                            "inference_unavailable",
                        )
                    else:
                        self._convert(
                            resource,
                            raw_path,
                            processed_path,
                            executable,
                            llama_server,
                            inference_url,
                        )
            except ProcessingInterrupted:
                resource.processing_state = "pending"
                resource.processing_error_type = "interrupted"
                resource.processed_at = None
                self._refresh_course_progress(course, resources, status="pending")
                course.last_processing_error_type = "interrupted"
                self.db.commit()
                logger.warning(
                    "Quercus course processing interrupted course_id=%s resource_id=%s",
                    course.course_id,
                    resource.resource_id,
                )
                raise
            self._refresh_course_progress(course, resources, status="running")

        successful = sum(resource.processing_state == "succeeded" for resource in resources)
        failed = sum(resource.processing_state == "failed" for resource in resources)
        course.last_processing_completed_at = utc_now()
        final_status = (
            "partial" if successful and failed else "failed" if failed else "succeeded"
        )
        self._refresh_course_progress(course, resources, status=final_status)
        course.last_processing_error_type = next(
            (resource.processing_error_type for resource in resources if resource.processing_state == "failed"),
            None,
        )
        self.db.commit()
        logger.info(
            "Quercus course processing finished course_id=%s status=%s processed=%s failed=%s",
            course.course_id,
            final_status,
            successful,
            failed,
        )
        return successful, failed

    def remove_processed(self, course_root: Path, resource: QuercusSyncResource) -> None:
        if resource.processed_relative_path:
            target = course_root / PurePosixPath(resource.processed_relative_path)
            if target.is_file():
                target.unlink()
        resource.processed_source_fingerprint = None
        resource.processing_state = "not_processed"
        resource.processing_error_type = None
        resource.processed_at = None

    def preferred_relative_path(self, resource: QuercusSyncResource) -> str | None:
        if self.method() == PROCESSING_MARKER and resource.processed_relative_path:
            course = self.db.get(QuercusCourse, resource.course_id)
            if course is not None:
                target = self._course_root(course) / PurePosixPath(resource.processed_relative_path)
                if target.is_file():
                    return resource.processed_relative_path
        return resource.relative_path

    def _convert(
        self,
        resource: QuercusSyncResource,
        raw_path: Path,
        processed_path: Path,
        executable: str,
        llama_server: Path,
        inference_url: str,
    ) -> None:
        temporary_root = QUERCUS_PROCESSING_ROOT
        temporary_root.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "SURYA_INFERENCE_BACKEND": "llamacpp",
                "SURYA_INFERENCE_URL": inference_url,
                "SURYA_INFERENCE_PARALLEL": "1",
                "LLAMA_CPP_BINARY": str(llama_server),
            }
        )
        environment["PATH"] = os.pathsep.join(
            [str(llama_server.parent), environment.get("PATH", "")]
        )
        error_type: str | None = None
        log_path: Path | None = None
        log_tail = ""
        try:
            with TemporaryDirectory(
                dir=temporary_root,
                prefix="marker-",
                ignore_cleanup_errors=True,
            ) as output_dir:
                arguments = [
                    executable,
                    str(raw_path),
                    "--mode",
                    "balanced",
                    "--output_format",
                    "markdown",
                    "--disable_image_extraction",
                    "--output_dir",
                    output_dir,
                ]
                log_path = Path(output_dir) / "marker.log"
                completed = self._run_marker(arguments, output_dir, environment, log_path)
                if completed.returncode != 0:
                    error_type = "marker_failed"
                    log_tail = self._read_log_tail(log_path)
                else:
                    markdown_files = list(Path(output_dir).rglob("*.md"))
                    if len(markdown_files) != 1:
                        error_type = "invalid_output"
                    else:
                        try:
                            content = markdown_files[0].read_text(encoding="utf-8")
                        except UnicodeError:
                            error_type = "invalid_output"
                        else:
                            if not content.strip():
                                error_type = "invalid_output"
                            else:
                                _atomic_write(processed_path, content.encode("utf-8"))
        except subprocess.TimeoutExpired:
            error_type = "timeout"
            logger.error(
                "Marker timed out course_id=%s resource_id=%s timeout_seconds=%s",
                resource.course_id,
                resource.resource_id,
                PROCESS_TIMEOUT_SECONDS,
            )
        except ProcessingInterrupted:
            raise
        except (OSError, ValueError):
            error_type = "marker_failed"
            logger.exception(
                "Marker execution failed course_id=%s resource_id=%s",
                resource.course_id,
                resource.resource_id,
            )
        if error_type is not None:
            logger.error(
                "Marker conversion failed course_id=%s resource_id=%s error_type=%s log_tail=%r",
                resource.course_id,
                resource.resource_id,
                error_type,
                log_tail,
            )
            self._record_failure(resource, processed_path, error_type)
            return
        resource.processed_source_fingerprint = resource.content_fingerprint
        resource.processed_by = PROCESSING_MARKER
        resource.processing_state = "succeeded"
        resource.processing_error_type = None
        resource.processed_at = utc_now()
        self.db.commit()
        logger.info(
            "Marker conversion succeeded course_id=%s resource_id=%s",
            resource.course_id,
            resource.resource_id,
        )

    def _run_marker(
        self,
        arguments: list[str],
        output_dir: str,
        environment: dict[str, str],
        log_path: Path,
    ) -> subprocess.CompletedProcess[Any]:
        if self.runner is not None:
            return self.runner(
                arguments,
                cwd=output_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
                check=False,
            )

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        started_at = time.monotonic()
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                arguments,
                cwd=output_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            logger.info(
                "Marker process started pid=%s parent_pid=%s output_dir=%s",
                process.pid,
                os.getpid(),
                output_dir,
            )
            while process.poll() is None:
                if self.stop_event is not None:
                    if self.stop_event.wait(0.25):
                        self._terminate_process_tree(process, reason="backend_shutdown")
                        logger.warning(
                            "Marker interrupted pid=%s log_tail=%r",
                            process.pid,
                            self._read_log_tail(log_path),
                        )
                        raise ProcessingInterrupted(
                            "Quercus processing interrupted by shutdown"
                        )
                else:
                    time.sleep(0.25)
                if time.monotonic() - started_at >= PROCESS_TIMEOUT_SECONDS:
                    self._terminate_process_tree(process, reason="timeout")
                    logger.error(
                        "Marker timeout pid=%s log_tail=%r",
                        process.pid,
                        self._read_log_tail(log_path),
                    )
                    raise subprocess.TimeoutExpired(arguments, PROCESS_TIMEOUT_SECONDS)
            logger.info(
                "Marker process exited pid=%s return_code=%s duration_seconds=%.1f",
                process.pid,
                process.returncode,
                time.monotonic() - started_at,
            )
            return subprocess.CompletedProcess(arguments, process.returncode)

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[Any], *, reason: str) -> None:
        if process.poll() is not None:
            return
        logger.warning("Stopping Marker process tree pid=%s reason=%s", process.pid, reason)
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                logger.warning(
                    "Could not stop Marker process tree pid=%s",
                    process.pid,
                    exc_info=True,
                )
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.error("Marker process tree did not exit pid=%s", process.pid)
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("Marker process tree did not exit pid=%s", process.pid)

    @staticmethod
    def _read_log_tail(log_path: Path | None, *, max_characters: int = 4000) -> str:
        if log_path is None or not log_path.is_file():
            return ""
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")[-max_characters:]
        except OSError:
            return ""

    def _record_failure(
        self, resource: QuercusSyncResource, processed_path: Path, error_type: str
    ) -> None:
        _atomic_write(processed_path, FAILURE_PLACEHOLDER.encode("utf-8"))
        resource.processed_source_fingerprint = resource.content_fingerprint
        resource.processed_by = PROCESSING_MARKER
        resource.processing_state = "failed"
        resource.processing_error_type = error_type
        resource.processed_at = utc_now()
        self.db.commit()

    def _refresh_course_progress(
        self,
        course: QuercusCourse,
        resources: list[QuercusSyncResource],
        *,
        status: str,
    ) -> None:
        course.processed_file_count = sum(
            resource.processing_state == "succeeded" for resource in resources
        )
        course.failed_processing_count = sum(
            resource.processing_state == "failed" for resource in resources
        )
        course.last_processing_status = status
        self.db.commit()

    def _raise_if_stopping(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise ProcessingInterrupted("Quercus processing interrupted by shutdown")

    def _recover_interrupted_locked(self, method: str) -> None:
        interrupted_resources = self.db.scalars(
            select(QuercusSyncResource).where(
                QuercusSyncResource.resource_type == "file",
                QuercusSyncResource.processing_state == "pending",
            )
        ).all()
        for resource in interrupted_resources:
            resource.processing_error_type = "interrupted"
            resource.processed_at = None

        recovered_courses = 0
        for course in self.db.scalars(select(QuercusCourse)).all():
            resources = self.db.scalars(
                select(QuercusSyncResource).where(
                    QuercusSyncResource.course_id == course.course_id,
                    QuercusSyncResource.resource_type == "file",
                    QuercusSyncResource.download_state == "downloaded",
                )
            ).all()
            if method == PROCESSING_NONE:
                status = "disabled"
            elif course.last_processing_status == "running" or any(
                resource.processing_state == "pending" for resource in resources
            ):
                status = "pending"
                course.last_processing_error_type = "interrupted"
                recovered_courses += 1
            else:
                status = course.last_processing_status or "pending"
            self._refresh_course_progress(course, resources, status=status)
        self.db.commit()
        if interrupted_resources or recovered_courses:
            logger.warning(
                "Recovered interrupted Quercus processing courses=%s resources=%s",
                recovered_courses,
                len(interrupted_resources),
            )

    def mark_worker_failed(self, error_type: str) -> None:
        for resource in self.db.scalars(
            select(QuercusSyncResource).where(
                QuercusSyncResource.resource_type == "file",
                QuercusSyncResource.processing_state == "pending",
            )
        ).all():
            resource.processing_error_type = error_type
        for course in self.db.scalars(
            select(QuercusCourse).where(
                QuercusCourse.last_processing_status == "running"
            )
        ).all():
            resources = self.db.scalars(
                select(QuercusSyncResource).where(
                    QuercusSyncResource.course_id == course.course_id,
                    QuercusSyncResource.resource_type == "file",
                    QuercusSyncResource.download_state == "downloaded",
                )
            ).all()
            course.last_processing_completed_at = utc_now()
            course.last_processing_error_type = error_type
            self._refresh_course_progress(course, resources, status="failed")
        self.db.commit()

    def _cleanup_temporary_output_locked(self) -> None:
        roots = {
            QUERCUS_PROCESSING_ROOT.resolve(),
            (self.workspace.workspace / "quercus-processing").resolve(),
        }
        for temporary_root in roots:
            if temporary_root.name != "quercus-processing" or not temporary_root.is_dir():
                continue
            for child in temporary_root.iterdir():
                if not child.is_dir() or not child.name.startswith(PROCESSING_TEMPORARY_PREFIXES):
                    continue
                try:
                    shutil.rmtree(child)
                    logger.info("Removed stale Quercus temporary directory path=%s", child)
                except OSError:
                    logger.warning(
                        "Could not remove stale Quercus temporary directory path=%s",
                        child,
                        exc_info=True,
                    )
            try:
                temporary_root.rmdir()
                logger.info("Removed empty Quercus temporary root path=%s", temporary_root)
            except OSError:
                # An active process or an unrelated entry means the root is not safe to remove.
                pass

    def _processed_path(self, course: QuercusCourse, resource: QuercusSyncResource) -> str:
        if resource.processed_relative_path:
            return resource.processed_relative_path
        raw_relative = PurePosixPath(resource.relative_path or "")
        try:
            tree = raw_relative.relative_to(PurePosixPath("files/raw"))
        except ValueError as exc:
            raise ValueError("Quercus raw resource path is invalid") from exc
        base = PurePosixPath("files/processed") / tree.parent / f"{tree.stem}.md"
        used = {
            str(value).casefold()
            for value in self.db.scalars(
                select(QuercusSyncResource.processed_relative_path).where(
                    QuercusSyncResource.course_id == course.course_id,
                    QuercusSyncResource.processed_relative_path.is_not(None),
                )
            ).all()
        }
        candidate = base
        number = 2
        while candidate.as_posix().casefold() in used:
            candidate = base.with_name(f"{base.stem}-{number}.md")
            number += 1
        resource.processed_relative_path = candidate.as_posix()
        self.db.commit()
        return resource.processed_relative_path

    def _migrate_layout_locked(self) -> None:
        for course in self.db.scalars(select(QuercusCourse)).all():
            course_root = self._course_root(course)
            (course_root / "files" / "raw").mkdir(parents=True, exist_ok=True)
            (course_root / "files" / "processed").mkdir(parents=True, exist_ok=True)
            replacements: list[tuple[str, str]] = []
            resources = self.db.scalars(
                select(QuercusSyncResource).where(
                    QuercusSyncResource.course_id == course.course_id,
                    QuercusSyncResource.resource_type == "file",
                )
            ).all()
            for resource in resources:
                old = resource.relative_path
                if not old or old.startswith("files/raw/"):
                    continue
                if not old.startswith("files/") or old.startswith("files/processed/"):
                    raise ValueError("Stored Quercus file path is invalid")
                new = f"files/raw/{old.removeprefix('files/')}"
                source = course_root / PurePosixPath(old)
                destination = course_root / PurePosixPath(new)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_file() and not destination.exists():
                    os.replace(source, destination)
                elif source.is_file() and destination.is_file():
                    if source.read_bytes() != destination.read_bytes():
                        raise ValueError("Conflicting Quercus raw migration target")
                    source.unlink()
                resource.relative_path = new
                replacements.append((old, new))
            self.db.commit()
            if replacements:
                self._rewrite_links(course_root, replacements)

    @staticmethod
    def _rewrite_links(course_root: Path, replacements: list[tuple[str, str]]) -> None:
        processed_root = course_root / "files" / "processed"
        for markdown_path in course_root.rglob("*.md"):
            if processed_root == markdown_path or processed_root in markdown_path.parents:
                continue
            relative_document = markdown_path.relative_to(course_root).as_posix()
            document_dir = PurePosixPath(relative_document).parent.as_posix()
            original = markdown_path.read_text(encoding="utf-8")
            updated = original
            for old, new in replacements:
                old_link = posixpath.relpath(old, document_dir)
                new_link = posixpath.relpath(new, document_dir)
                updated = updated.replace(old_link, new_link)
            if updated != original:
                _atomic_write(markdown_path, updated.encode("utf-8"))

    def _course_root(self, course: QuercusCourse) -> Path:
        root = self.workspace.quercus.resolve()
        course_root = (root / course.local_path).resolve()
        if course_root.parent != root:
            raise ValueError("Stored Quercus course path is invalid")
        return course_root

    @staticmethod
    def _normalize_llama_cpp_directory(value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        directory = Path(value.strip()).expanduser()
        if not directory.is_absolute():
            raise ValueError("The llama.cpp directory must be an absolute path")
        directory = directory.resolve()
        executable = directory / ("llama-server.exe" if os.name == "nt" else "llama-server")
        if not directory.is_dir() or not executable.is_file():
            raise ValueError(
                "The llama.cpp directory must exist and contain llama-server.exe"
            )
        return str(directory)

    @staticmethod
    def _llama_server_path(directory: str | None) -> Path | None:
        if not directory:
            return None
        root = Path(directory)
        executable = root / ("llama-server.exe" if os.name == "nt" else "llama-server")
        if root.is_dir() and executable.is_file():
            return executable.resolve()
        return None

    def _inference_state(self) -> tuple[bool, str | None]:
        if self.inference_probe is not None:
            return self.inference_probe(), INFERENCE_URL
        sentinel = Path.home() / ".cache" / "datalab" / "surya" / "llamacpp_server.json"
        try:
            data = json.loads(sentinel.read_text(encoding="utf-8"))
            port = int(data["port"])
            if not (1 <= port <= 65535):
                return False, None
            inference_url = f"http://127.0.0.1:{port}/v1"
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ) as response:
                return 200 <= response.status < 300, inference_url
        except (
            KeyError,
            json.JSONDecodeError,
            OSError,
            TypeError,
            urllib.error.URLError,
            ValueError,
        ):
            return False, None


class QuercusProcessingDispatcher:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending = False
        self._running = False

    def request(self) -> None:
        with self._lock:
            if self._stop_event.is_set():
                logger.warning("Ignored Quercus processing request during shutdown")
                return
            self._pending = True
            if self._running:
                logger.info("Coalesced Quercus processing request")
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run,
                name="quercus-processing",
                daemon=True,
            )
            thread = self._thread
        logger.info("Starting Quercus processing worker")
        thread.start()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def stop(self, timeout_seconds: float = 30) -> None:
        with self._lock:
            self._pending = False
            self._stop_event.set()
            thread = self._thread
        if thread is None or not thread.is_alive():
            logger.info("Quercus processing worker already stopped")
            return
        logger.warning(
            "Stopping Quercus processing worker thread_id=%s timeout_seconds=%s",
            thread.ident,
            timeout_seconds,
        )
        thread.join(timeout_seconds)
        if thread.is_alive():
            logger.error(
                "Quercus processing worker did not stop within timeout thread_id=%s",
                thread.ident,
            )
        else:
            logger.info("Quercus processing worker stopped")

    def _run(self) -> None:
        logger.info(
            "Quercus processing worker entered pid=%s parent_pid=%s thread_id=%s",
            os.getpid(),
            os.getppid(),
            threading.get_ident(),
        )
        try:
            while True:
                with self._lock:
                    if self._stop_event.is_set() or not self._pending:
                        return
                    self._pending = False
                try:
                    with self.session_factory() as db:
                        QuercusProcessingService(db, stop_event=self._stop_event).run()
                except ProcessingInterrupted:
                    logger.warning("Quercus processing worker interrupted by shutdown")
                    return
                except Exception:
                    logger.exception("Quercus processing worker failed unexpectedly")
                    try:
                        with self.session_factory() as recovery_db:
                            QuercusProcessingService(recovery_db).mark_worker_failed(
                                "processing_worker_failed"
                            )
                    except Exception:
                        logger.exception("Could not persist Quercus worker failure state")
        finally:
            with self._lock:
                self._running = False
                self._thread = None
            logger.info("Quercus processing worker exited")
