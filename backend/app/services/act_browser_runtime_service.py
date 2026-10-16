from __future__ import annotations

import ctypes
import io
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import threading
from pathlib import Path

from app.services.browser_authentication_bridge import browser_authentication_bridge_endpoint

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ActBrowserRuntimeError(RuntimeError):
    pass


def _pipe_request(endpoint: str, request: bytes, timeout: float) -> bytes:
    """Bound synchronous Windows pipe IO, including a stalled owner."""
    completed: queue.Queue = queue.Queue(maxsize=1)

    def exchange() -> None:
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            wait_pipe = kernel.WaitNamedPipeW
            wait_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            wait_pipe.restype = ctypes.c_int
            if not wait_pipe(endpoint, int(timeout * 1000)):
                raise OSError("pipe unavailable")
            with open(endpoint, "r+b", buffering=0) as pipe:  # noqa: PTH123
                pipe.write(request)
                completed.put(io.BufferedReader(pipe).readline(MAX_RESPONSE_BYTES + 1))
        except Exception:
            completed.put(None)

    worker = threading.Thread(target=exchange, daemon=True)
    worker.start()
    try:
        result = completed.get(timeout=timeout)
    except queue.Empty:
        # Cancel the pending read without terminating unrelated owner requests.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenThread.restype = ctypes.c_void_p
        kernel.CancelSynchronousIo.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenThread(0x0001, False, worker.native_id)
        if handle:
            try:
                kernel.CancelSynchronousIo(handle)
            finally:
                kernel.CloseHandle(handle)
        raise ActBrowserRuntimeError("Shared Act browser is unavailable") from None
    if result is None:
        raise ActBrowserRuntimeError("Shared Act browser is unavailable")
    return result


def browser_runtime_request(endpoint: str, token: str, payload: dict, *, timeout: float = 15) -> dict:
    """One private connection per request; concurrent sessions never share a stream."""
    try:
        if not endpoint or not token:
            raise ValueError("missing endpoint")
        request = json.dumps({**payload, "token": token}, separators=(",", ":")).encode() + b"\n"
        if len(request) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        if os.name == "nt" and endpoint.startswith("\\\\.\\pipe\\"):
            response = _pipe_request(endpoint, request, timeout)
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(endpoint)
                client.sendall(request)
                with client.makefile("rb") as reader:
                    response = reader.readline(MAX_RESPONSE_BYTES + 1)
        if not response or len(response) > MAX_RESPONSE_BYTES or not response.endswith(b"\n"):
            raise ValueError("invalid response")
        parsed = json.loads(response)
        if not isinstance(parsed, dict):
            raise ValueError("invalid response")
        return parsed
    except Exception:
        # IPC can carry credentials. Never expose an exception containing a payload.
        raise ActBrowserRuntimeError("Shared Act browser is unavailable") from None


def playwright_module_path() -> Path:
    explicit = os.environ.get("EIDOLON_PLAYWRIGHT_MODULE_PATH")
    if explicit:
        path = Path(explicit).resolve()
        if path.is_file():
            return path
        raise ActBrowserRuntimeError("Configured Playwright MCP module is unavailable")
    cache_roots = [Path.home() / ".npm"]
    if os.environ.get("LOCALAPPDATA"):
        cache_roots.insert(0, Path(os.environ["LOCALAPPDATA"]) / "npm-cache")
    if os.environ.get("npm_config_cache"):
        cache_roots.insert(0, Path(os.environ["npm_config_cache"]))
    modules = [path for root in cache_roots for path in root.glob("_npx/*/node_modules/@playwright/mcp/index.js")]
    if not modules:
        raise ActBrowserRuntimeError("Shared Act browser requires an already installed Playwright MCP package")
    return max(modules, key=lambda path: path.stat().st_mtime).resolve()


class ActBrowserRuntimeService:
    """Backend-owned browser lifetime; agent restarts only replace client tokens."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._endpoint = ""
        self._owner_token = ""

    def _start(self, workspace_root: Path) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            self.stop()
        node = shutil.which("node")
        if not node:
            raise ActBrowserRuntimeError("Shared Act browser requires Node.js")
        module = playwright_module_path()
        owner_token = secrets.token_urlsafe(32)
        profile = Path.home() / ".eidolon" / "browser-profiles" / "act-shared"
        profile.mkdir(parents=True, exist_ok=True)
        workspace_root = Path(workspace_root).resolve()
        workspace_root.mkdir(parents=True, exist_ok=True)
        endpoint = browser_authentication_bridge_endpoint(owner_token, workspace_root)
        env = os.environ.copy()
        for key in ("EIDOLON_AGENT_TOKEN", "EIDOLON_BROWSER_AUTH_TOKEN"):
            env.pop(key, None)
        env.update({
            "EIDOLON_PLAYWRIGHT_MODULE_PATH": str(module),
            "EIDOLON_BROWSER_OWNER_TOKEN": owner_token,
            "EIDOLON_BROWSER_AUTH_ENDPOINT": endpoint,
            "EIDOLON_BROWSER_PROFILE": str(profile),
            "EIDOLON_BROWSER_WORKSPACE": str(workspace_root),
        })
        try:
            process = subprocess.Popen(
                [node, str(Path(__file__).parents[1] / "act_browser_runtime.mjs")],
                cwd=workspace_root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            raise ActBrowserRuntimeError("Shared Act browser could not start") from None
        self._process, self._endpoint, self._owner_token = process, endpoint, owner_token
        ready: queue.Queue = queue.Queue(maxsize=1)

        def read_ready() -> None:
            try:
                line = process.stdout.readline(4097)
                ready.put(len(line) <= 4096 and json.loads(line).get("ready") is True)
            except Exception:
                ready.put(False)

        def discard_stderr() -> None:
            # Provider stderr may contain locators, page text, or secrets.
            while process.stderr.read(4096):
                pass

        threading.Thread(target=read_ready, daemon=True).start()
        threading.Thread(target=discard_stderr, daemon=True).start()
        try:
            initialized = ready.get(timeout=45)
        except queue.Empty:
            initialized = False
        if not initialized or process.poll() is not None:
            self.stop()
            raise ActBrowserRuntimeError("Shared Act browser could not start; check Playwright MCP compatibility")

    def register_session(self, session_id: int, token: str, workspace_root: Path) -> str:
        with self._lock:
            self._start(workspace_root)
            endpoint, owner_token = self._endpoint, self._owner_token
        result = browser_runtime_request(endpoint, owner_token, {
            "action": "register", "sessionId": session_id, "sessionToken": token,
        })
        if result.get("ok") is not True:
            raise ActBrowserRuntimeError("Shared Act browser could not register this conversation")
        return endpoint

    def _session_action(self, action: str, session_id: int) -> None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return
            endpoint, owner_token = self._endpoint, self._owner_token
        result = browser_runtime_request(endpoint, owner_token, {"action": action, "sessionId": session_id})
        if result.get("ok") is not True:
            raise ActBrowserRuntimeError("Shared Act browser is unavailable")

    def revoke_session(self, session_id: int) -> None:
        self._session_action("revoke", session_id)

    def close_session(self, session_id: int) -> None:
        self._session_action("close_session", session_id)

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process, self._endpoint, self._owner_token = None, "", ""
            if process is None:
                return
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                else:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()


act_browser_runtime = ActBrowserRuntimeService()
