import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from app.schemas.codex_routing import ResolvedInvocationSettings
from app.services.codex_cli_service import codex_cli_service, should_use_real_codex
from app.services.codex_routing_service import CodexRoutingError, CodexRoutingService


@dataclass
class ProjectPlausibilityResult:
    plausible: bool
    reason: str


class ProjectPlausibilityAdapter(Protocol):
    def evaluate(self, prompt: str, message: str) -> ProjectPlausibilityResult:
        pass


class FakeProjectPlausibilityAdapter:
    def evaluate(self, prompt: str, message: str) -> ProjectPlausibilityResult:
        return ProjectPlausibilityResult(
            plausible=True,
            reason="Fake Codex plausibility adapter accepts the project request for local tests.",
        )


class RealProjectPlausibilityAdapter:
    def __init__(
        self,
        command: str | None = None,
        timeout_seconds: int | None = None,
        workdir: Path | None = None,
        sandbox_mode: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.command = command or codex_cli_service.command()
        self.timeout_seconds = timeout_seconds or _env_int("PERSONAL_AGENT_CODEX_PLAUSIBILITY_TIMEOUT_SECONDS", 120)
        self.workdir = workdir or Path.cwd()
        self.sandbox_mode = sandbox_mode or os.getenv("PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX", "read-only")
        self.approval_policy = approval_policy or os.getenv("PERSONAL_AGENT_CODEX_APPROVAL_POLICY", "never")
        self.model = model if model is not None else os.getenv("PERSONAL_AGENT_CODEX_MODEL")
        self.reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else os.getenv("PERSONAL_AGENT_CODEX_REASONING_EFFORT")
        )

    def evaluate(self, prompt: str, message: str) -> ProjectPlausibilityResult:
        command = [
            self.command,
            "--ask-for-approval",
            self.approval_policy,
        ]
        if self.reasoning_effort:
            command.extend(["--config", f'model_reasoning_effort="{self.reasoning_effort}"'])
        command.extend([
            "exec",
            "-C",
            str(self.workdir),
            "--ephemeral",
            "--color",
            "never",
            "--sandbox",
            self.sandbox_mode,
        ])
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        result = subprocess.run(
            command,
            cwd=self.workdir,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            shell=False,
        )
        if result.returncode != 0:
            return ProjectPlausibilityResult(
                plausible=False,
                reason=result.stderr.strip() or "Codex could not evaluate the project request.",
            )
        return self.parse_result(result.stdout)

    def with_invocation_settings(self, settings: ResolvedInvocationSettings) -> "RealProjectPlausibilityAdapter":
        return RealProjectPlausibilityAdapter(
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            workdir=self.workdir,
            sandbox_mode=self.sandbox_mode,
            approval_policy=self.approval_policy,
            model=settings.effective_model,
            reasoning_effort=settings.effective_reasoning_effort,
        )

    def parse_result(self, stdout: str) -> ProjectPlausibilityResult:
        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError:
            return ProjectPlausibilityResult(
                plausible=False,
                reason="Codex returned an unreadable plausibility review.",
            )
        return ProjectPlausibilityResult(
            plausible=bool(data.get("plausible")),
            reason=str(data.get("reason") or "No reason provided."),
        )


def default_project_plausibility_adapter() -> ProjectPlausibilityAdapter:
    if should_use_real_codex():
        return RealProjectPlausibilityAdapter(workdir=Path(__file__).resolve().parents[3])
    return FakeProjectPlausibilityAdapter()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class ProjectPlausibilityService:
    adapter: ProjectPlausibilityAdapter | None = None
    db: Session | None = None

    def __post_init__(self) -> None:
        if self.adapter is None:
            self.adapter = default_project_plausibility_adapter()

    def evaluate(self, message: str) -> ProjectPlausibilityResult:
        prompt = self.build_prompt(message)
        adapter = self.adapter
        if isinstance(adapter, RealProjectPlausibilityAdapter) and self.db is not None:
            try:
                settings = CodexRoutingService(self.db).resolve(
                    role="product_manager", action="project_plausibility"
                )
            except CodexRoutingError as exc:
                return ProjectPlausibilityResult(plausible=False, reason=str(exc), optional_projects=[])
            adapter = adapter.with_invocation_settings(settings)
        return adapter.evaluate(prompt, message)

    def build_prompt(self, message: str) -> str:
        return f"""
You are reviewing whether a user's Project mode request should become an application skill proposal.

Application skill definition:
- A skill is a reusable capability package with executable Python code and tests.
- A skill may include optional SKILL.md reusable instructions or operating guidance.

Evaluate only plausibility and fit. Do not generate files. Do not install packages. Do not run code.

Return only JSON with this shape:
{{
  "plausible": true,
  "reason": "short plain-English reason"
}}

A plausible project should be reusable, inspectable, bounded, and possible within the MVP safety model.
Reject one-off factual questions, vague requests without a reusable workflow, and requests that require prohibited actions.
When rejecting, explain why and suggest a safer or better-scoped alternative in the reason.

User request:
{message}
""".strip()
