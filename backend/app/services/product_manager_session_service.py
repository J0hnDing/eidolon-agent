from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerRequestTimeout,
    CodexAppServerSubscription,
    codex_app_server_client,
)


class ProductManagerSessionError(RuntimeError):
    """A ProductManager App Server session could not complete safely."""


class ProductManagerTurnTimeout(ProductManagerSessionError):
    """A ProductManager turn exceeded its configured deadline."""


@dataclass(frozen=True)
class ProductManagerThreadMetadata:
    thread_id: str
    model: str | None
    reasoning_effort: str | None


@dataclass(frozen=True)
class ProductManagerTurnResult:
    output_text: str
    thread_id: str
    turn_id: str
    usage: dict[str, int]
    model: str | None
    reasoning_effort: str | None
    items: list[dict[str, Any]]
    events: list[dict[str, Any]]


class ProductManagerSessionService:
    """Runs structured ProductManager turns in resumable App Server threads."""

    def __init__(self, client: CodexAppServerClient | None = None) -> None:
        self.client = client or codex_app_server_client
        self._threads: dict[str, ProductManagerThreadMetadata] = {}
        self._threads_lock = threading.Lock()

    def start_thread(
        self,
        *,
        cwd: str | Path | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        developer_instructions: str | None = None,
        base_instructions: str | None = None,
        sandbox: str = "read-only",
        approval_policy: str = "never",
        config: dict[str, Any] | None = None,
        permissions: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "cwd": str(Path(cwd).resolve()) if cwd is not None else str(Path.cwd().resolve()),
            "model": model,
            "developerInstructions": developer_instructions,
            "baseInstructions": base_instructions,
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
            "ephemeral": False,
        }
        if reasoning_effort:
            params["config"] = {"model_reasoning_effort": reasoning_effort}
        if config is not None:
            params["config"] = {**params.get("config", {}), **config}
        if permissions is not None:
            params.pop("sandbox", None)
            params["permissions"] = permissions
        response = self.client.request("thread/start", params)
        metadata = self._metadata(response)
        with self._threads_lock:
            self._threads[metadata.thread_id] = metadata
        return metadata.thread_id

    def resume_thread(
        self,
        thread_id: str,
        *,
        cwd: str | Path | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        developer_instructions: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        config: dict[str, Any] | None = None,
        permissions: str | None = None,
    ) -> ProductManagerThreadMetadata:
        params: dict[str, Any] = {"threadId": thread_id}
        if cwd is not None:
            params["cwd"] = str(Path(cwd).resolve())
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["config"] = {"model_reasoning_effort": reasoning_effort}
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions
        if sandbox is not None:
            params["sandbox"] = sandbox
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if config is not None:
            params["config"] = {**params.get("config", {}), **config}
        if permissions is not None:
            params.pop("sandbox", None)
            params["permissions"] = permissions
        response = self.client.request("thread/resume", params)
        metadata = self._metadata(response, expected_thread_id=thread_id)
        with self._threads_lock:
            self._threads[thread_id] = metadata
        return metadata

    def has_thread(self, thread_id: str) -> bool:
        """Return whether this App Server process already owns the thread."""
        with self._threads_lock:
            return thread_id in self._threads

    def run_structured_turn(
        self,
        thread_id: str,
        input_text: str,
        output_schema: dict[str, Any],
        *,
        timeout_seconds: float,
        model: str | None = None,
        reasoning_effort: str | None = None,
        on_turn_started: Callable[[str], None] | None = None,
    ) -> ProductManagerTurnResult:
        if not input_text.strip():
            raise ValueError("ProductManager turn input must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("ProductManager turn timeout must be positive")

        deadline = time.monotonic() + timeout_seconds
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": input_text}],
            "outputSchema": output_schema,
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort

        with self.client.subscribe() as events:
            try:
                response = self.client.request(
                    "turn/start",
                    params,
                    timeout=max(0.001, deadline - time.monotonic()),
                )
            except CodexAppServerRequestTimeout as exc:
                turn_id = self._started_turn_id(events, thread_id)
                if turn_id:
                    self._interrupt_best_effort(thread_id, turn_id)
                raise ProductManagerTurnTimeout("ProductManager turn timed out while starting") from exc

            turn = response.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise ProductManagerSessionError("Codex App Server returned an invalid turn")
            turn_id = turn["id"]
            if on_turn_started is not None:
                on_turn_started(turn_id)
            try:
                result = self._wait_for_turn(
                    events,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    deadline=deadline,
                    initial_items=turn.get("items"),
                )
            except (CodexAppServerRequestTimeout, TimeoutError) as exc:
                self._interrupt_best_effort(thread_id, turn_id)
                raise ProductManagerTurnTimeout("ProductManager turn timed out") from exc

        with self._threads_lock:
            metadata = self._threads.get(thread_id)
        effective_model = model or (metadata.model if metadata else None)
        effective_effort = reasoning_effort or (metadata.reasoning_effort if metadata else None)
        if model or reasoning_effort:
            with self._threads_lock:
                self._threads[thread_id] = ProductManagerThreadMetadata(
                    thread_id=thread_id,
                    model=effective_model,
                    reasoning_effort=effective_effort,
                )
        return ProductManagerTurnResult(
            output_text=result["output_text"],
            thread_id=thread_id,
            turn_id=turn_id,
            usage=result["usage"],
            model=effective_model,
            reasoning_effort=effective_effort,
            items=result["items"],
            events=result["events"],
        )

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.client.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            timeout=10,
        )

    def archive_thread(self, thread_id: str) -> None:
        self.client.request("thread/archive", {"threadId": thread_id}, timeout=10)
        with self._threads_lock:
            self._threads.pop(thread_id, None)

    def _wait_for_turn(
        self,
        subscription: CodexAppServerSubscription,
        *,
        thread_id: str,
        turn_id: str,
        deadline: float,
        initial_items: Any,
    ) -> dict[str, Any]:
        items_by_id: dict[str, dict[str, Any]] = {}
        if isinstance(initial_items, list):
            for item in initial_items:
                self._store_item(items_by_id, item)
        usage = self._empty_usage()
        transcript: list[dict[str, Any]] = []
        terminal_error: str | None = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            event = subscription.get(timeout=remaining)
            method = event.get("method")
            params = event.get("params")
            if not isinstance(params, dict) or not self._matches_turn(params, thread_id, turn_id):
                continue
            transcript.append(event)

            if method == "item/completed":
                self._store_item(items_by_id, params.get("item"))
            elif method == "thread/tokenUsage/updated":
                usage = self._normalize_usage(params.get("tokenUsage"))
            elif method == "error" and not params.get("willRetry", False):
                terminal_error = self._turn_error(params.get("error"))
            elif method == "turn/completed":
                turn = params.get("turn")
                if not isinstance(turn, dict):
                    raise ProductManagerSessionError("Codex App Server returned an invalid completion event")
                completed_items = turn.get("items")
                if isinstance(completed_items, list):
                    for item in completed_items:
                        self._store_item(items_by_id, item)
                status = turn.get("status")
                if status != "completed":
                    detail = self._turn_error(turn.get("error")) or terminal_error or str(status or "failed")
                    raise ProductManagerSessionError(f"ProductManager turn did not complete: {detail}")
                output_text = self._final_agent_message(list(items_by_id.values()))
                if not output_text:
                    raise ProductManagerSessionError("ProductManager turn completed without a final response")
                return {
                    "output_text": output_text,
                    "usage": usage,
                    "items": list(items_by_id.values()),
                    "events": transcript,
                }

    def _interrupt_best_effort(self, thread_id: str, turn_id: str) -> None:
        try:
            self.interrupt_turn(thread_id, turn_id)
        except (OSError, RuntimeError, CodexAppServerError):
            pass

    @staticmethod
    def _metadata(
        response: dict[str, Any],
        *,
        expected_thread_id: str | None = None,
    ) -> ProductManagerThreadMetadata:
        thread = response.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise ProductManagerSessionError("Codex App Server returned an invalid thread")
        if expected_thread_id is not None and thread_id != expected_thread_id:
            raise ProductManagerSessionError("Codex App Server resumed a different thread")
        model = response.get("model")
        effort = response.get("reasoningEffort")
        return ProductManagerThreadMetadata(
            thread_id=thread_id,
            model=model if isinstance(model, str) and model else None,
            reasoning_effort=effort if isinstance(effort, str) and effort else None,
        )

    @staticmethod
    def _matches_turn(params: dict[str, Any], thread_id: str, turn_id: str) -> bool:
        event_thread_id = params.get("threadId")
        event_turn_id = params.get("turnId")
        if event_turn_id is None and isinstance(params.get("turn"), dict):
            event_turn_id = params["turn"].get("id")
        return event_thread_id == thread_id and event_turn_id == turn_id

    @staticmethod
    def _store_item(items: dict[str, dict[str, Any]], value: Any) -> None:
        if not isinstance(value, dict):
            return
        item_id = value.get("id")
        if isinstance(item_id, str) and item_id:
            items[item_id] = value

    @staticmethod
    def _final_agent_message(items: list[dict[str, Any]]) -> str:
        unknown_phase: list[str] = []
        final_messages: list[str] = []
        for item in items:
            if item.get("type") != "agentMessage" or not isinstance(item.get("text"), str):
                continue
            text = item["text"].strip()
            if not text:
                continue
            phase = item.get("phase")
            if phase == "final_answer":
                final_messages.append(text)
            elif phase is None:
                unknown_phase.append(text)
        candidates = final_messages or unknown_phase
        return candidates[-1] if candidates else ""

    @classmethod
    def _normalize_usage(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict) or not isinstance(value.get("last"), dict):
            return cls._empty_usage()
        last = value["last"]

        def token(name: str) -> int:
            raw = last.get(name, 0)
            return max(0, int(raw)) if isinstance(raw, (int, float)) else 0

        return {
            "input_tokens": token("inputTokens"),
            "cached_input_tokens": token("cachedInputTokens"),
            "output_tokens": token("outputTokens"),
            "reasoning_output_tokens": token("reasoningOutputTokens"),
            "total_tokens": token("totalTokens"),
        }

    @staticmethod
    def _empty_usage() -> dict[str, int]:
        return {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }

    @staticmethod
    def _turn_error(value: Any) -> str | None:
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
        if isinstance(value, str) and value:
            return value
        return None

    @staticmethod
    def _started_turn_id(subscription: CodexAppServerSubscription, thread_id: str) -> str | None:
        while True:
            try:
                event = subscription.get(timeout=0.001)
            except (CodexAppServerRequestTimeout, CodexAppServerError):
                return None
            params = event.get("params")
            if event.get("method") != "turn/started" or not isinstance(params, dict):
                continue
            turn = params.get("turn")
            if params.get("threadId") == thread_id and isinstance(turn, dict) and isinstance(turn.get("id"), str):
                return turn["id"]


product_manager_session_service = ProductManagerSessionService()
