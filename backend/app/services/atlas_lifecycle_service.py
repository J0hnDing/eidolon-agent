from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AtlasLifecycleError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


@dataclass
class AtlasHttpResult:
    status: int
    body: dict[str, Any]


class AtlasHttpClient:
    base_url = "http://127.0.0.1:4817"
    timeout_seconds = 3.0
    max_response_bytes = 128 * 1024

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        api_key: str | None = None,
    ) -> AtlasHttpResult:
        if not path.startswith("/api/") or "://" in path:
            raise AtlasLifecycleError("invalid_request", "Atlas route is invalid")
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json", "Host": "127.0.0.1:4817"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            response = urllib.request.build_opener(_NoRedirect).open(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            with exc:
                return AtlasHttpResult(exc.code, self._read_json(exc))
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise AtlasLifecycleError("atlas_unavailable", "Atlas is unavailable") from exc
        with response:
            return AtlasHttpResult(response.status, self._read_json(response))

    def _read_json(self, response) -> dict[str, Any]:  # noqa: ANN001
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > self.max_response_bytes:
                    raise AtlasLifecycleError("response_too_large", "Atlas response exceeded the limit")
            except ValueError as exc:
                raise AtlasLifecycleError("invalid_response", "Atlas returned an invalid response") from exc
        raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise AtlasLifecycleError("response_too_large", "Atlas response exceeded the limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AtlasLifecycleError("invalid_response", "Atlas returned an invalid response") from exc
        if not isinstance(value, dict):
            raise AtlasLifecycleError("invalid_response", "Atlas returned an invalid response")
        return value


class AtlasLifecycleService:
    def __init__(
        self,
        *,
        project_root: Path | None = None,
        client: AtlasHttpClient | None = None,
        readiness_seconds: float = 8.0,
    ) -> None:
        self.project_root = (project_root or Path(__file__).resolve().parents[3]).resolve()
        self.config_path = self.project_root / "runtime" / "atlas_integration.json"
        self.client = client or AtlasHttpClient()
        self.readiness_seconds = readiness_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self.ownership = "none"
        self.startup_error: str | None = None
        self.auto_unlock_attempted = False

    @property
    def directory(self) -> Path:
        configured = self._read_config().get("directory")
        if isinstance(configured, str) and Path(configured).is_absolute():
            return Path(configured).resolve()
        return (self.project_root.parent / "Eidolon-Atlas").resolve()

    def save_directory(self, directory: Path) -> None:
        resolved = directory.resolve()
        if not resolved.is_absolute():
            raise AtlasLifecycleError("invalid_directory", "Atlas directory must be absolute")
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"directory": str(resolved)}, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.config_path)

    def validate_directory(self, directory: Path) -> Path:
        resolved = directory.resolve()
        self._validated_launch(resolved)
        return resolved

    def start(self) -> None:
        self.startup_error = None
        self.auto_unlock_attempted = False
        try:
            status = self.client.request("GET", "/api/status")
            if status.status == 200 and self._valid_status(status.body):
                self.ownership = "external"
                return
        except AtlasLifecycleError:
            pass
        try:
            directory, node = self._validated_launch(self.directory)
            self._process = subprocess.Popen(
                [node, "src/index.js"],
                cwd=directory,
                env={**os.environ, "ATLAS_PORT": "4817"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            self.ownership = "owned"
            deadline = time.monotonic() + self.readiness_seconds
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise AtlasLifecycleError("startup_failed", "Atlas exited during startup")
                try:
                    result = self.client.request("GET", "/api/status")
                    if result.status == 200 and self._valid_status(result.body):
                        return
                except AtlasLifecycleError:
                    pass
                time.sleep(0.05)
            raise AtlasLifecycleError("startup_timeout", "Atlas did not become ready in time")
        except AtlasLifecycleError as exc:
            self.startup_error = str(exc)
            self.stop()
            raise
        except OSError as exc:
            self.startup_error = "Atlas process could not be started"
            self.stop()
            raise AtlasLifecycleError("startup_failed", self.startup_error) from exc

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self.ownership = "none"

    def restart(self) -> None:
        if self.ownership == "owned":
            self.stop()
        else:
            self.ownership = "none"
        self.start()

    def status(self) -> dict[str, Any]:
        if self._process is not None and self._process.poll() is not None:
            self._process = None
            self.ownership = "none"
        try:
            result = self.client.request("GET", "/api/status")
        except AtlasLifecycleError:
            return {"running": False, "initialized": None, "locked": None}
        if result.status != 200 or not self._valid_status(result.body):
            return {"running": False, "initialized": None, "locked": None}
        return {"running": True, "initialized": result.body["initialized"], "locked": result.body["locked"]}

    def validate_api_key(self, api_key: str) -> str:
        result = self.client.request("GET", "/api/agent/tools", api_key=api_key)
        if result.status == 200 and result.body.get("project") == "Eidolon-Atlas":
            return "connected"
        if result.status == 423:
            return "locked_recognized"
        if result.status == 401:
            raise AtlasLifecycleError("invalid_api_key", "Atlas API key is invalid")
        raise AtlasLifecycleError("atlas_unavailable", "Atlas API key could not be validated")

    def unlock(self, passphrase: str) -> None:
        result = self.client.request("POST", "/api/unlock", body={"passphrase": passphrase})
        if result.status == 200 and self._valid_status(result.body) and result.body["locked"] is False:
            return
        code = str(result.body.get("error", {}).get("code", ""))
        error_type = {
            "NOT_INITIALIZED": "atlas_uninitialized",
            "INVALID_PASSPHRASE": "invalid_passphrase",
        }.get(code, "unlock_failed")
        raise AtlasLifecycleError(error_type, "Atlas could not be unlocked")

    def _validated_launch(self, directory: Path) -> tuple[Path, str]:
        if not directory.is_dir() or not (directory / "package.json").is_file() or not (directory / "src/index.js").is_file():
            raise AtlasLifecycleError("invalid_directory", "Atlas directory is invalid")
        node = shutil.which("node")
        if not node:
            raise AtlasLifecycleError("node_unavailable", "Node.js is unavailable")
        try:
            version = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=3, check=True
            ).stdout.strip().removeprefix("v")
            major = int(version.split(".", 1)[0])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise AtlasLifecycleError("node_unavailable", "Node.js version could not be checked") from exc
        if major < 24:
            raise AtlasLifecycleError("node_incompatible", "Atlas requires Node.js 24 or newer")
        return directory, node

    def _read_config(self) -> dict[str, Any]:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _valid_status(value: dict[str, Any]) -> bool:
        return isinstance(value.get("initialized"), bool) and isinstance(value.get("locked"), bool)


atlas_lifecycle_service = AtlasLifecycleService()
