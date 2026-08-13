from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from typing import Any

import pytest

from app.services.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerRequestTimeout,
)
from app.services.product_manager_session_service import (
    ProductManagerSessionError,
    ProductManagerSessionService,
    ProductManagerTurnTimeout,
)


class _FakeStdout:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def readline(self) -> str:
        line = self.lines.get(timeout=2)
        return line or ""

    def publish(self, message: dict[str, Any]) -> None:
        self.lines.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self.lines.put(None)


class _FakeStdin:
    def __init__(self, on_message: Callable[[dict[str, Any]], None]) -> None:
        self._on_message = on_message
        self._buffer = ""

    def write(self, value: str) -> int:
        self._buffer += value
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                self._on_message(json.loads(line))
        return len(value)

    def flush(self) -> None:
        pass


class _ScriptedProcess:
    def __init__(self, handler: Callable[[_ScriptedProcess, dict[str, Any]], None]) -> None:
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(lambda message: handler(self, message))
        self._closed = False

    def respond(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        self.stdout.publish({"id": request["id"], "result": result})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.stdout.publish({"method": method, "params": params})

    def poll(self) -> int | None:
        return 0 if self._closed else None

    def terminate(self) -> None:
        if not self._closed:
            self._closed = True
            self.stdout.close()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.terminate()


def _initialize(process: _ScriptedProcess, request: dict[str, Any]) -> bool:
    if request.get("method") == "initialize":
        process.respond(request, {"userAgent": "test"})
        return True
    return request.get("method") == "initialized"


def test_app_server_correlates_interleaved_responses_and_routes_notifications(monkeypatch) -> None:
    requests: list[tuple[_ScriptedProcess, dict[str, Any]]] = []
    lock = threading.Lock()

    def handler(process: _ScriptedProcess, request: dict[str, Any]) -> None:
        if _initialize(process, request):
            return
        with lock:
            requests.append((process, request))
            if len(requests) != 2:
                return
            first_process, first = requests[0]
            second_process, second = requests[1]
            process.notify("account/rateLimits/updated", {"rateLimits": {"primary": {}}})
            second_process.respond(second, {"name": second["method"]})
            first_process.respond(first, {"name": first["method"]})

    process = _ScriptedProcess(handler)
    monkeypatch.setattr("app.services.codex_app_server.subprocess.Popen", lambda *args, **kwargs: process)
    client = CodexAppServerClient("codex")
    results: dict[str, dict[str, Any]] = {}

    with client.subscribe() as subscription:
        threads = [
            threading.Thread(target=lambda: results.update(account=client.request("account/rateLimits/read", None))),
            threading.Thread(target=lambda: results.update(models=client.request("model/list", {}))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        event = subscription.get(timeout=1)

    assert results["account"] == {"name": "account/rateLimits/read"}
    assert results["models"] == {"name": "model/list"}
    assert event["method"] == "account/rateLimits/updated"
    client.stop()


def test_app_server_restarts_process_before_resuming_persisted_thread(monkeypatch) -> None:
    processes: list[_ScriptedProcess] = []

    def factory(*_args: Any, **_kwargs: Any) -> _ScriptedProcess:
        process_number = len(processes)

        def handler(process: _ScriptedProcess, request: dict[str, Any]) -> None:
            if _initialize(process, request):
                return
            if request["method"] == "thread/start":
                process.respond(request, {"thread": {"id": "thread-1"}, "model": "gpt-test", "reasoningEffort": "high"})
                process.terminate()
            elif request["method"] == "thread/resume":
                assert process_number == 1
                process.respond(request, {"thread": {"id": "thread-1"}, "model": "gpt-test", "reasoningEffort": "high"})
            else:
                raise AssertionError(request)

        process = _ScriptedProcess(handler)
        processes.append(process)
        return process

    monkeypatch.setattr("app.services.codex_app_server.subprocess.Popen", factory)
    service = ProductManagerSessionService(CodexAppServerClient("codex"))

    thread_id = service.start_thread(model="gpt-test", reasoning_effort="high")
    metadata = service.resume_thread(thread_id)

    assert len(processes) == 2
    assert metadata.thread_id == "thread-1"
    assert metadata.model == "gpt-test"
    assert metadata.reasoning_effort == "high"
    service.client.stop()


class _FakeSubscription:
    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()

    def get(self, timeout: float | None = None) -> dict[str, Any]:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexAppServerRequestTimeout("event timeout") from exc

    def __enter__(self) -> _FakeSubscription:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


class _FakeSessionClient:
    def __init__(self, turn_events: list[dict[str, Any]] | None = None) -> None:
        self.turn_events = turn_events or []
        self.requests: list[tuple[str, Any]] = []
        self.subscription: _FakeSubscription | None = None

    def subscribe(self) -> _FakeSubscription:
        self.subscription = _FakeSubscription()
        return self.subscription

    def request(self, method: str, params: Any, *, timeout: float = 30) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}, "model": "gpt-test", "reasoningEffort": "high"}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}, "model": "gpt-test", "reasoningEffort": "high"}
        if method == "turn/start":
            assert self.subscription is not None
            for event in self.turn_events:
                self.subscription.events.put(event)
            return {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}
        if method in {"turn/interrupt", "thread/archive"}:
            return {}
        raise AssertionError(method)


def _event(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"method": method, "params": params}


def test_product_manager_turn_extracts_final_message_and_per_turn_usage() -> None:
    usage = {
        "last": {
            "inputTokens": 120,
            "cachedInputTokens": 30,
            "outputTokens": 40,
            "reasoningOutputTokens": 10,
            "totalTokens": 160,
        },
        "total": {
            "inputTokens": 999,
            "cachedInputTokens": 999,
            "outputTokens": 999,
            "reasoningOutputTokens": 999,
            "totalTokens": 1998,
        },
    }
    final_item = {"id": "final", "type": "agentMessage", "phase": "final_answer", "text": '{"decision":"ask"}'}
    client = _FakeSessionClient(
        [
            _event("item/completed", {"threadId": "other", "turnId": "turn-x", "item": final_item}),
            _event(
                "item/completed",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "note", "type": "agentMessage", "phase": "commentary", "text": "Working"},
                },
            ),
            _event(
                "item/completed",
                {"threadId": "thread-1", "turnId": "turn-1", "item": final_item},
            ),
            _event(
                "thread/tokenUsage/updated",
                {"threadId": "thread-1", "turnId": "turn-1", "tokenUsage": usage},
            ),
            _event(
                "turn/completed",
                {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}},
            ),
        ]
    )
    service = ProductManagerSessionService(client)  # type: ignore[arg-type]
    service.start_thread(model="gpt-test", reasoning_effort="high")

    result = service.run_structured_turn(
        "thread-1",
        "Plan this",
        {"type": "object"},
        timeout_seconds=1,
    )

    assert result.output_text == '{"decision":"ask"}'
    assert result.usage == {
        "input_tokens": 120,
        "cached_input_tokens": 30,
        "output_tokens": 40,
        "reasoning_output_tokens": 10,
        "total_tokens": 160,
    }
    assert result.model == "gpt-test"
    assert result.reasoning_effort == "high"
    turn_params = next(params for method, params in client.requests if method == "turn/start")
    assert turn_params["outputSchema"] == {"type": "object"}


def test_product_manager_failed_turn_raises() -> None:
    client = _FakeSessionClient(
        [
            _event(
                "error",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": False,
                    "error": {"message": "provider failed"},
                },
            ),
            _event(
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "failed", "error": {"message": "provider failed"}, "items": []},
                },
            ),
        ]
    )
    service = ProductManagerSessionService(client)  # type: ignore[arg-type]

    with pytest.raises(ProductManagerSessionError, match="provider failed"):
        service.run_structured_turn("thread-1", "Plan", {"type": "object"}, timeout_seconds=1)


def test_product_manager_timeout_interrupts_active_turn() -> None:
    client = _FakeSessionClient()
    service = ProductManagerSessionService(client)  # type: ignore[arg-type]

    with pytest.raises(ProductManagerTurnTimeout):
        service.run_structured_turn("thread-1", "Plan", {"type": "object"}, timeout_seconds=0.01)

    assert ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}) in client.requests


def test_product_manager_resume_and_archive_use_thread_contracts() -> None:
    client = _FakeSessionClient()
    service = ProductManagerSessionService(client)  # type: ignore[arg-type]

    metadata = service.resume_thread("thread-1")
    service.archive_thread("thread-1")

    assert metadata.thread_id == "thread-1"
    assert ("thread/resume", {"threadId": "thread-1"}) in client.requests
    assert ("thread/archive", {"threadId": "thread-1"}) in client.requests
