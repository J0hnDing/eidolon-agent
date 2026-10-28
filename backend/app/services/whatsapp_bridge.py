"""Private, single-owner stdio transport for the optional WhatsApp child process."""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_FRAME = 4 * 1024 * 1024


class WhatsAppError(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__("WhatsApp operation could not be completed")
        self.error_type = error_type


class WhatsAppBridge:
    def __init__(self, root: Path | None = None, *, timeout: float = 30) -> None:
        self.root = (root or Path(__file__).resolve().parents[3]).resolve()
        self.timeout = timeout
        self.lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._process: subprocess.Popen | None = None
        self.generation: str | None = None
        self.snapshot: dict[str, Any] = {}
        self.error_type: str | None = None

    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, *, key: str, generation: str) -> None:
        with self.lock:
            if self.running() and self.generation == generation:
                return
            self.stop()
            directory = self.root / "bridges" / "whatsapp"
            executable = self.root / "runtime" / "bin" / (
                "eidolon-whatsapp-bridge.exe" if sys.platform == "win32" else "eidolon-whatsapp-bridge"
            )
            if not executable.is_file():
                self.error_type = "dependencies_missing"
                raise WhatsAppError(self.error_type)
            try:
                process = subprocess.Popen(
                    [str(executable)], cwd=directory, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False,
                )
                self._process = process
                self.generation = generation
                self.error_type = None
                threading.Thread(target=self._read, args=(process,), daemon=True).start()
                self.request("initialize", {
                    "key": key, "directory": str(self.root / "runtime" / "whatsapp" / generation),
                    "generation": generation,
                })
            except (OSError, WhatsAppError):
                self.stop()
                self.error_type = "startup_failed"
                raise WhatsAppError(self.error_type) from None

    def _read(self, process: subprocess.Popen) -> None:
        try:
            while process.stdout:
                line = process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    raise ValueError
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError
                with self._state_lock:
                    if self._process is not process:
                        return
                    if value.get("event") == "status" and isinstance(value.get("data"), dict):
                        self.snapshot = value["data"]
                    target = self._pending.get(value.get("id"))
                    if target:
                        target.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            with self._state_lock:
                if self._process is process:
                    self.snapshot = {}
                    self.error_type = "bridge_unavailable"
                    for target in self._pending.values():
                        try:
                            target.put_nowait({"error": {"code": "bridge_unavailable"}})
                        except queue.Full:
                            pass
            if process.poll() is None:
                process.terminate()

    def request(self, method: str, input_json: dict | None = None) -> dict:
        with self.lock:
            if not self.running():
                raise WhatsAppError("bridge_unavailable")
            identifier = uuid4().hex
            frame = (json.dumps({"id": identifier, "method": method, "input": input_json or {}},
                                separators=(",", ":")) + "\n").encode()
            if len(frame) > MAX_FRAME:
                raise WhatsAppError("invalid_input")
            response: queue.Queue = queue.Queue(maxsize=1)
            with self._state_lock:
                self._pending[identifier] = response
            dispatched = False
            is_send = method == "execute" and (input_json or {}).get("operation") == "whatsapp.message.send"
            try:
                process = self._process
                assert process is not None and process.stdin is not None
                # A partial write can already have reached the bridge.
                dispatched = True
                process.stdin.write(frame)
                process.stdin.flush()
                value = response.get(timeout=self.timeout)
                if "error" in value:
                    error = value["error"]
                    code = error.get("code", "internal_failure") if isinstance(error, dict) else "invalid_response"
                    if is_send and code == "bridge_unavailable":
                        code = "outcome_unknown"
                    raise WhatsAppError(code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
                                        else "internal_failure")
                result = value.get("result")
                if not isinstance(result, dict):
                    raise WhatsAppError("invalid_response")
                return result
            except (OSError, queue.Empty):
                raise WhatsAppError("outcome_unknown" if is_send and dispatched else "provider_timeout") from None
            finally:
                with self._state_lock:
                    self._pending.pop(identifier, None)

    def stop(self) -> None:
        with self.lock:
            process, self._process = self._process, None
            self.generation = None
            self.snapshot = {}
            if process is None:
                return
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()


whatsapp_bridge = WhatsAppBridge()
