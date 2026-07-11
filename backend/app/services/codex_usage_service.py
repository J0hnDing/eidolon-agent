from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from typing import Any


class CodexUsageService:
    """Persistent, local Codex App Server client for account allowance data."""

    def __init__(self, command: str | None = None) -> None:
        self.command = command or os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            try:
                self._start_locked()
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
                self._last_error = str(exc)
                self._process = None

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def read_account_usage(self) -> dict[str, Any]:
        try:
            payload = self._request("account/rateLimits/read", None)
            snapshot = self._select_codex_snapshot(payload)
            return {
                "available": True,
                "source": "codex_app_server",
                "fetched_at": datetime.now(UTC).isoformat(),
                "plan_type": snapshot.get("planType"),
                "limit_id": snapshot.get("limitId") or "codex",
                "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
                "five_hour": self._window(snapshot.get("primary"), "5-hour"),
                "weekly": self._window(snapshot.get("secondary"), "weekly"),
            }
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            self._last_error = str(exc)
            return {
                "available": False,
                "source": "codex_app_server",
                "fetched_at": datetime.now(UTC).isoformat(),
                "error": self._last_error,
                "plan_type": None,
                "limit_id": "codex",
                "rate_limit_reached_type": None,
                "five_hour": None,
                "weekly": None,
            }

    def should_pause_workflow(self, minimum_remaining_percent: int = 5) -> tuple[bool, dict[str, Any]]:
        usage = self.read_account_usage()
        if not usage["available"]:
            return False, usage
        windows = [usage.get("five_hour"), usage.get("weekly")]
        below_reserve = bool(usage.get("rate_limit_reached_type")) or any(
            window and window.get("remaining_percent", 100) < minimum_remaining_percent for window in windows
        )
        return below_reserve, usage

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._process = subprocess.Popen(
            [self.command, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        response = self._send_locked(
            "initialize",
            {
                "clientInfo": {"name": "personal-agent", "title": "Personal Agent", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        if "error" in response:
            raise RuntimeError(str(response["error"]))
        self._write_locked({"method": "initialized"})

    def _request(self, method: str, params: Any) -> dict[str, Any]:
        with self._lock:
            self._start_locked()
            response = self._send_locked(method, params)
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex App Server returned an invalid response")
            return result

    def _send_locked(self, method: str, params: Any) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._write_locked({"id": request_id, "method": method, "params": params})
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("Codex App Server is not running")
        while True:
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("Codex App Server closed its output")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def _write_locked(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Codex App Server is not running")
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    @staticmethod
    def _select_codex_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
        by_id = payload.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
            return by_id["codex"]
        snapshot = payload.get("rateLimits")
        if not isinstance(snapshot, dict):
            raise ValueError("Codex allowance data is unavailable")
        return snapshot

    @staticmethod
    def _window(value: Any, label: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        used = max(0, min(100, int(value.get("usedPercent", 0))))
        resets_at = value.get("resetsAt")
        return {
            "label": label,
            "used_percent": used,
            "remaining_percent": 100 - used,
            "window_duration_minutes": value.get("windowDurationMins"),
            "resets_at": datetime.fromtimestamp(resets_at, UTC).isoformat() if isinstance(resets_at, int) else None,
        }


codex_usage_service = CodexUsageService()
