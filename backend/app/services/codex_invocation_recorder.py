from __future__ import annotations

import subprocess
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SkillRun


class CodexInvocationRecorder:
    """Keep build usage buffering separate from per-skill runtime usage persistence."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._pending_build_invocations: list[dict[str, object]] = []
        self._pending_build_transcripts: list[dict[str, str]] = []

    @staticmethod
    def from_result(
        result: subprocess.CompletedProcess[str],
        plan: dict[str, object],
        *,
        default_adapter_name: str,
    ) -> dict[str, object] | None:
        usage = getattr(result, "codex_usage", None)
        if not isinstance(usage, dict):
            return None
        return {
            "action": plan.get("codex_task") or plan.get("action") or "codex_invocation",
            "adapter": getattr(result, "codex_adapter", default_adapter_name),
            "status": "succeeded",
            "requested_model": getattr(result, "codex_requested_model", None) or plan.get("model"),
            "effective_model": getattr(result, "codex_model", None) or plan.get("model"),
            "model": getattr(result, "codex_model", None) or plan.get("model"),
            "requested_reasoning_effort": getattr(result, "codex_requested_reasoning_effort", None)
            or plan.get("reasoning_effort"),
            "effective_reasoning_effort": getattr(result, "codex_reasoning_effort", None)
            or plan.get("reasoning_effort"),
            "route_source": getattr(result, "codex_route_source", None),
            "role": getattr(result, "codex_role", None),
            "difficulty": getattr(result, "codex_difficulty", None),
            "cli_path": getattr(result, "codex_cli_path", None),
            "cli_version": getattr(result, "codex_cli_version", None),
            "cli_source": getattr(result, "codex_cli_source", None),
            "thread_id": getattr(result, "codex_thread_id", None),
            "turn_id": getattr(result, "codex_turn_id", None),
            **usage,
        }

    def record_build_result(
        self,
        result: subprocess.CompletedProcess[str],
        plan: dict[str, object],
        *,
        default_adapter_name: str,
        prompt: str,
    ) -> None:
        self._pending_build_transcripts.append(
            {
                "action": str(plan.get("codex_task") or plan.get("action") or "codex_invocation"),
                "input": prompt,
                "output": result.stdout or "",
            }
        )
        invocation = self.from_result(result, plan, default_adapter_name=default_adapter_name)
        if invocation is not None:
            self._pending_build_invocations.append(invocation)

    def record_build(self, invocation: dict[str, object]) -> None:
        self._pending_build_invocations.append(invocation)

    def consume_build_usage(self) -> list[dict[str, object]]:
        invocations = self._pending_build_invocations
        self._pending_build_invocations = []
        return invocations

    def consume_build_transcripts(self) -> list[dict[str, str]]:
        transcripts = self._pending_build_transcripts
        self._pending_build_transcripts = []
        return transcripts

    def record_skill_runtime(self, skill_id: int, invocation: dict[str, object]) -> None:
        run = self.db.scalar(
            select(SkillRun)
            .where(SkillRun.skill_id == skill_id, SkillRun.status == "running")
            .order_by(SkillRun.started_at.desc(), SkillRun.id.desc())
        )
        if run is None:
            return
        run.codex_invocations_json = [*(run.codex_invocations_json or []), invocation]
        for field_name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            setattr(run, field_name, getattr(run, field_name) + int(invocation.get(field_name, 0)))
        if invocation.get("status") == "failed":
            message = str(invocation.get("error_message") or "Codex runtime call failed")
            run.error_message = f"Codex runtime call failed: {message}"
        self.db.commit()

    @staticmethod
    def error_detail(stderr: str | None, *, returncode: int | None = None) -> str:
        lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
        error_lines = [line for line in lines if line.lower().startswith("error:")]
        detail = "\n".join(error_lines[-5:] or lines[-10:])
        if not detail:
            detail = "Codex CLI failed without diagnostic output"
        if len(detail) > 2000:
            detail = detail[-2000:]
        if returncode is not None:
            return f"Codex CLI exited with code {returncode}: {detail}"
        return detail

    @staticmethod
    def failed_skill_runtime(
        plan: dict[str, object],
        *,
        adapter: object,
        is_real_adapter: bool,
        cli_status: Any = None,
        error_type: str,
        error_message: str,
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> dict[str, object]:
        return {
            "action": plan.get("codex_task") or "skill_runtime_codex",
            "adapter": "codex_cli" if is_real_adapter else type(adapter).__name__,
            "status": "failed",
            "requested_model": plan.get("model"),
            "effective_model": getattr(adapter, "model", None) or plan.get("model"),
            "model": getattr(adapter, "model", None) or plan.get("model"),
            "requested_reasoning_effort": plan.get("reasoning_effort"),
            "effective_reasoning_effort": getattr(adapter, "reasoning_effort", None)
            or plan.get("reasoning_effort"),
            "route_source": "legacy_default",
            "role": None,
            "difficulty": None,
            "cli_path": cli_status.resolved_path if cli_status else None,
            "cli_version": cli_status.version if cli_status else None,
            "cli_source": cli_status.source if cli_status else None,
            "exit_code": returncode,
            "error_type": error_type,
            "error_message": error_message,
            "stderr_tail": (stderr or "")[-4000:] or None,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }
