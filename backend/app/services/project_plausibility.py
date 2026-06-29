import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class ProjectPlausibilityResult:
    plausible: bool
    reason: str
    optional_projects: list[str]


class ProjectPlausibilityAdapter(Protocol):
    def evaluate(self, prompt: str, message: str) -> ProjectPlausibilityResult:
        pass


class FakeProjectPlausibilityAdapter:
    def evaluate(self, prompt: str, message: str) -> ProjectPlausibilityResult:
        normalized = message.lower().strip()
        if len(normalized) < 12:
            return ProjectPlausibilityResult(
                plausible=False,
                reason="The request is too short to turn into a useful reusable skill proposal.",
                optional_projects=[
                    "Describe the repeated workflow you want to reuse.",
                    "Describe an instruction, automation, or hybrid skill with expected inputs and outputs.",
                ],
            )
        if any(term in normalized for term in ("what is ", "explain ", "define ")):
            return ProjectPlausibilityResult(
                plausible=False,
                reason="This reads like a one-off question rather than a reusable capability package.",
                optional_projects=[
                    "Create a reusable instruction for answering this kind of question.",
                    "Create an automation that transforms recurring inputs into a structured output.",
                ],
            )
        return ProjectPlausibilityResult(
            plausible=True,
            reason="The request is plausible as a reusable application skill proposal.",
            optional_projects=[],
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
    ) -> None:
        self.command = command or os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
        self.timeout_seconds = timeout_seconds or _env_int("PERSONAL_AGENT_CODEX_PLAUSIBILITY_TIMEOUT_SECONDS", 120)
        self.workdir = workdir or Path.cwd()
        self.sandbox_mode = sandbox_mode or os.getenv("PERSONAL_AGENT_CODEX_PLAUSIBILITY_SANDBOX", "read-only")
        self.approval_policy = approval_policy or os.getenv("PERSONAL_AGENT_CODEX_APPROVAL_POLICY", "never")
        self.model = model if model is not None else os.getenv("PERSONAL_AGENT_CODEX_MODEL")

    def evaluate(self, prompt: str, message: str) -> ProjectPlausibilityResult:
        command = [
            self.command,
            "--ask-for-approval",
            self.approval_policy,
            "exec",
            "-C",
            str(self.workdir),
            "--ephemeral",
            "--color",
            "never",
            "--sandbox",
            self.sandbox_mode,
        ]
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
                optional_projects=["Try a more specific reusable skill request."],
            )
        return self.parse_result(result.stdout)

    def parse_result(self, stdout: str) -> ProjectPlausibilityResult:
        try:
            data = json.loads(stdout.strip())
        except json.JSONDecodeError:
            return ProjectPlausibilityResult(
                plausible=False,
                reason="Codex returned an unreadable plausibility review.",
                optional_projects=["Try again with a clearer project request."],
            )
        return ProjectPlausibilityResult(
            plausible=bool(data.get("plausible")),
            reason=str(data.get("reason") or "No reason provided."),
            optional_projects=[str(item) for item in data.get("optional_projects", [])],
        )


def default_project_plausibility_adapter() -> ProjectPlausibilityAdapter:
    if should_use_real_codex():
        return RealProjectPlausibilityAdapter(workdir=Path(__file__).resolve().parents[3])
    return FakeProjectPlausibilityAdapter()


def should_use_real_codex() -> bool:
    mode = os.getenv("PERSONAL_AGENT_CODEX_MODE", "auto").strip().lower()
    if mode == "real":
        return True
    if mode in {"fake", "dev", "stub", "local"}:
        return False
    command = os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
    return shutil.which(command) is not None


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

    def __post_init__(self) -> None:
        if self.adapter is None:
            self.adapter = default_project_plausibility_adapter()

    def evaluate(self, message: str) -> ProjectPlausibilityResult:
        prompt = self.build_prompt(message)
        return self.adapter.evaluate(prompt, message)

    def build_prompt(self, message: str) -> str:
        return f"""
You are reviewing whether a user's Project mode request should become an application skill proposal.

Application skill definition:
- A skill is a reusable capability package.
- instruction skills are reusable instructions only.
- automation skills contain executable Python automation.
- hybrid skills contain both reusable instructions and executable Python automation.

Evaluate only plausibility and fit. Do not generate files. Do not install packages. Do not run code.

Return only JSON with this shape:
{{
  "plausible": true,
  "reason": "short plain-English reason",
  "optional_projects": ["alternative project idea if not plausible"]
}}

A plausible project should be reusable, inspectable, bounded, and possible within the MVP safety model.
Reject one-off factual questions, vague requests without a reusable workflow, and requests that require prohibited actions.
When rejecting, explain why and suggest safer or better-scoped optional projects.

User request:
{message}
""".strip()
