from __future__ import annotations

import json
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from app.services.codex_cli_service import codex_cli_service


class CodexAppServerError(RuntimeError):
    """Base error raised by the local Codex App Server transport."""


class CodexAppServerConnectionError(CodexAppServerError):
    """The App Server process or its stdio connection ended unexpectedly."""


class CodexAppServerRequestTimeout(CodexAppServerError):
    """A JSON-RPC response was not received before its request timeout."""


@dataclass
class _PendingResponse:
    event: threading.Event
    response: dict[str, Any] | None = None
    error: BaseException | None = None


class CodexAppServerSubscription:
    """A bounded notification stream tied to one App Server connection."""

    def __init__(self, client: CodexAppServerClient, generation: int, *, max_events: int) -> None:
        self._client = client
        self.generation = generation
        self._events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=max_events)
        self._closed = False

    def get(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            item = self._events.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexAppServerRequestTimeout("Timed out waiting for a Codex App Server event") from exc
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client._unsubscribe(self)

    def __enter__(self) -> CodexAppServerSubscription:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def _publish(self, item: dict[str, Any] | BaseException) -> None:
        if self._closed:
            return
        try:
            self._events.put_nowait(item)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(item)
            except queue.Full:
                pass


class CodexAppServerClient:
    """Thread-safe JSON-RPC client for one persistent local App Server process."""

    def __init__(self, command: str | None = None) -> None:
        self.command = command
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._generation = 0
        self._reader_error: BaseException | None = None
        self._request_id = 0
        self._pending: dict[int, _PendingResponse] = {}
        self._orphan_responses: dict[int, dict[str, Any]] = {}
        self._subscriptions: set[CodexAppServerSubscription] = set()
        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def start(self) -> None:
        with self._lifecycle_lock:
            self._ensure_started_locked()

    def stop(self) -> None:
        with self._lifecycle_lock:
            process, self._process = self._process, None
            self._reader_error = CodexAppServerConnectionError("Codex App Server was stopped")
            self._fail_generation(self._generation, self._reader_error)
            if process is None:
                return
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def request(self, method: str, params: Any, *, timeout: float = 30) -> dict[str, Any]:
        self.start()
        return self._request_running(method, params, timeout=timeout)

    def subscribe(self, *, max_events: int = 2048) -> CodexAppServerSubscription:
        self.start()
        with self._state_lock:
            subscription = CodexAppServerSubscription(self, self._generation, max_events=max_events)
            self._subscriptions.add(subscription)
            return subscription

    def _ensure_started_locked(self) -> None:
        process = self._process
        reader = self._reader
        if (
            process is not None
            and process.poll() is None
            and reader is not None
            and reader.is_alive()
            and self._reader_error is None
        ):
            return

        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass

        command = self.command or codex_cli_service.command()
        process = subprocess.Popen(
            [command, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        with self._state_lock:
            self._process = process
            self._generation += 1
            generation = self._generation
            self._reader_error = None
            self._request_id = 0
            self._orphan_responses.clear()
        self._reader = threading.Thread(
            target=self._read_messages,
            args=(process, generation),
            name="eidolon-codex-app-server",
            daemon=True,
        )
        self._reader.start()
        try:
            self._request_running(
                "initialize",
                {
                    "clientInfo": {"name": "eidolon", "title": "Eidolon", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=15,
            )
            self._write({"method": "initialized"}, generation=generation)
        except BaseException:
            try:
                process.terminate()
            except OSError:
                pass
            raise

    def _request_running(self, method: str, params: Any, *, timeout: float) -> dict[str, Any]:
        with self._state_lock:
            generation = self._generation
            self._request_id += 1
            request_id = self._request_id
            pending = _PendingResponse(threading.Event())
            orphan = self._orphan_responses.pop(request_id, None)
            if orphan is not None:
                pending.response = orphan
                pending.event.set()
            else:
                self._pending[request_id] = pending
        try:
            self._write({"id": request_id, "method": method, "params": params}, generation=generation)
            if not pending.event.wait(timeout=max(0.001, timeout)):
                raise CodexAppServerRequestTimeout(f"Timed out waiting for {method}")
            if pending.error is not None:
                raise pending.error
            response = pending.response
            if not isinstance(response, dict):
                raise CodexAppServerConnectionError("Codex App Server returned an invalid response")
            if "error" in response:
                raise CodexAppServerError(self._error_message(response["error"]))
            result = response.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerError("Codex App Server returned an invalid response")
            return result
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def _write(self, message: dict[str, Any], *, generation: int) -> None:
        with self._write_lock:
            with self._state_lock:
                process = self._process
                current_generation = self._generation
                reader_error = self._reader_error
            if generation != current_generation or reader_error is not None:
                raise CodexAppServerConnectionError("Codex App Server connection changed")
            if process is None or process.stdin is None:
                raise CodexAppServerConnectionError("Codex App Server is not running")
            try:
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                error = CodexAppServerConnectionError("Could not write to Codex App Server")
                self._fail_generation(generation, error)
                raise error from exc

    def _read_messages(self, process: subprocess.Popen[str], generation: int) -> None:
        if process.stdout is None:
            self._fail_generation(generation, CodexAppServerConnectionError("Codex App Server has no output stream"))
            return
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    raise CodexAppServerConnectionError("Codex App Server closed its output")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                response_id = message.get("id")
                if isinstance(response_id, int) and "method" not in message:
                    self._route_response(generation, response_id, message)
                else:
                    self._route_event(generation, message)
        except BaseException as exc:
            error = (
                exc
                if isinstance(exc, CodexAppServerConnectionError)
                else CodexAppServerConnectionError(f"Codex App Server reader failed: {exc}")
            )
            self._fail_generation(generation, error)

    def _route_response(self, generation: int, request_id: int, response: dict[str, Any]) -> None:
        with self._state_lock:
            if generation != self._generation:
                return
            pending = self._pending.get(request_id)
            if pending is None:
                if len(self._orphan_responses) >= 32:
                    self._orphan_responses.pop(next(iter(self._orphan_responses)))
                self._orphan_responses[request_id] = response
                return
            pending.response = response
            pending.event.set()

    def _route_event(self, generation: int, message: dict[str, Any]) -> None:
        with self._state_lock:
            subscriptions = [item for item in self._subscriptions if item.generation == generation]
        for subscription in subscriptions:
            subscription._publish(message)

    def _fail_generation(self, generation: int, error: BaseException) -> None:
        with self._state_lock:
            if generation != self._generation:
                return
            self._reader_error = error
            pending = list(self._pending.values())
            subscriptions = [item for item in self._subscriptions if item.generation == generation]
        for item in pending:
            if item.response is None:
                item.error = error
                item.event.set()
        for subscription in subscriptions:
            subscription._publish(error)

    def _unsubscribe(self, subscription: CodexAppServerSubscription) -> None:
        with self._state_lock:
            self._subscriptions.discard(subscription)

    @staticmethod
    def _error_message(error: Any) -> str:
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
            return json.dumps(error, sort_keys=True)
        return str(error)


codex_app_server_client = CodexAppServerClient()
