import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.schemas.common import InterfaceType, RiskLevel, SkillType
from app.schemas.manifest import ManifestPermissions


SAFE_SKILL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


class SkillPlanError(ValueError):
    pass


class SkillGenerationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    skill_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(min_length=1)
    skill_type: SkillType
    interface_type: InterfaceType = "chat"
    files_to_generate: list[str]
    expected_input: dict[str, Any]
    expected_output: dict[str, Any]
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    tool_ui_schema: dict[str, Any] | None = None
    requested_permissions: ManifestPermissions
    requested_network_domains: list[str]
    requested_dependencies: list[str]
    schedule: dict[str, Any] | None = None
    tests_required: bool
    validation_steps: list[str]
    risk_level: RiskLevel
    automatic_actions_blocked: list[str]

    @field_validator("requested_dependencies")
    @classmethod
    def validate_requested_dependencies(cls, dependencies: list[str]) -> list[str]:
        for dependency in dependencies:
            normalized = dependency.strip()
            lowered = normalized.lower()
            if (
                not normalized
                or "://" in normalized
                or "/" in normalized
                or "\\" in normalized
                or lowered.startswith("git+")
                or normalized.startswith("-")
                or "@" in normalized
                or ";" in normalized
            ):
                raise ValueError("requested_dependencies must contain only package names or simple version specifiers")
        return dependencies

    @model_validator(mode="after")
    def validate_plan_contract(self) -> "SkillGenerationPlan":
        required_files = {"manifest.json", "README.md"}
        if self.skill_type == "instruction":
            required_files.add("SKILL.md")
        if self.skill_type == "automation":
            required_files.update({"skill.py", "tests/test_skill.py"})
        missing = required_files - set(self.files_to_generate)
        if missing:
            raise ValueError(f"files_to_generate missing required files: {sorted(missing)}")
        if self.skill_type == "instruction" and self.tests_required:
            raise ValueError("instruction skill plans must not require tests")
        if self.skill_type == "automation" and not self.tests_required:
            raise ValueError(f"{self.skill_type} skill plans must require tests")
        if self.skill_type == "instruction":
            if not (
                self.requested_permissions.network == []
                and self.requested_permissions.filesystem_read == []
                and self.requested_permissions.filesystem_write == []
                and self.requested_permissions.secrets == []
                and self.requested_permissions.shell is False
            ):
                raise ValueError("instruction skill plans must request no permissions")
            if self.interface_type == "tool":
                raise ValueError("instruction skills cannot use tool interface_type")
        if self.requested_network_domains != self.requested_permissions.network:
            raise ValueError("requested_network_domains must match requested_permissions.network")
        return self


class SkillPlanAdapter(Protocol):
    def build_plan(self, prompt: str, message: str) -> dict[str, Any]:
        pass


class FakeSkillPlanAdapter:
    def build_plan(self, prompt: str, message: str) -> dict[str, Any]:
        identity = _infer_skill_identity(message)
        schedule = _infer_schedule(message)
        return {
            "goal": message,
            "skill_name": identity["skill_name"],
            "display_name": identity["display_name"],
            "skill_type": "automation",
            "interface_type": "chat",
            "files_to_generate": ["manifest.json", "README.md", "skill.py", "tests/test_skill.py"],
            "expected_input": {"input": "object"},
            "expected_output": {
                "title": "string",
                "items": [],
                "warnings": [],
                "schedule": schedule,
            },
            "input_schema": None,
            "output_schema": None,
            "tool_ui_schema": None,
            "requested_permissions": {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
            "requested_network_domains": [],
            "requested_dependencies": [],
            "tests_required": True,
            "validation_steps": [
                "validate manifest.json",
                "inspect generated files",
                "run tests for automation skills",
            ],
            "risk_level": "low",
            "automatic_actions_blocked": [
                "installing the skill",
                "running the skill",
                "installing packages without approval",
            ],
            "schedule": schedule,
        }


class RealSkillPlanAdapter:
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
        self.timeout_seconds = timeout_seconds or _env_int("PERSONAL_AGENT_CODEX_PLAN_TIMEOUT_SECONDS", 120)
        self.workdir = workdir or Path.cwd()
        self.sandbox_mode = sandbox_mode or os.getenv("PERSONAL_AGENT_CODEX_PLAN_SANDBOX", "read-only")
        self.approval_policy = approval_policy or os.getenv("PERSONAL_AGENT_CODEX_APPROVAL_POLICY", "never")
        self.model = model if model is not None else os.getenv("PERSONAL_AGENT_CODEX_MODEL")

    def build_plan(self, prompt: str, message: str) -> dict[str, Any]:
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
            raise SkillPlanError(result.stderr.strip() or "Codex could not build a skill generation plan")
        return parse_json_object(result.stdout)


def default_skill_plan_adapter() -> SkillPlanAdapter:
    if should_use_real_codex():
        return RealSkillPlanAdapter(workdir=Path(__file__).resolve().parents[3])
    return FakeSkillPlanAdapter()


def should_use_real_codex() -> bool:
    mode = os.getenv("PERSONAL_AGENT_CODEX_MODE", "auto").strip().lower()
    if mode == "real":
        return True
    if mode in {"fake", "dev", "stub", "local"}:
        return False
    command = os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
    return shutil.which(command) is not None


@dataclass
class SkillPlanService:
    adapter: SkillPlanAdapter | None = None

    def __post_init__(self) -> None:
        if self.adapter is None:
            self.adapter = default_skill_plan_adapter()

    def build_generation_plan(self, message: str) -> dict[str, Any]:
        raw_plan = self.adapter.build_plan(self.build_prompt(message), message)
        raw_plan.setdefault("goal", message)
        try:
            plan = SkillGenerationPlan.model_validate(raw_plan)
        except ValidationError as exc:
            raise SkillPlanError(str(exc)) from exc
        return plan.model_dump(mode="json")

    def build_prompt(self, message: str) -> str:
        return f"""
You are designing an application skill generation plan for the Local-First Self-Extending Personal AI Assistant.

Return only one JSON object. Do not write files. Do not install packages. Do not run code.

Definitions:
- A skill is a reusable capability package.
- skill_type must be exactly one of: instruction, automation.
- instruction: reusable instructions only, no executable code.
- automation: executable Python automation.
- Automation skills may include SKILL.md for reusable instructions or operating notes, but SKILL.md is optional for automation.
- interface_type must be exactly one of: chat, tool, hidden.
- interface_type=tool means an installed runnable automation skill should appear on the Tools page as a manual form/tool. It is not a new skill_type.
- interface_type=chat means the skill is primarily used through chat.
- interface_type=hidden means it should not be user-facing by default.
- Tool UIs must be declarative JSON in tool_ui_schema. Do not generate React, HTML, JavaScript, or frontend app code.
- For interface_type=tool, tool_ui_schema should describe a simple form the app can safely render.
- Supported tool_ui_schema field types: text, number, textarea, checkbox, select.

Safety requirements:
- shell must be false.
- secrets must be [].
- instruction skills must request no permissions and cannot use interface_type=tool.
- automation skills must include tests/test_skill.py.
- Use filesystem_write ["./cache"] only when useful; otherwise [].
- Use explicit network domains only when the user request genuinely needs future runtime network access.
- Do not request package dependencies unless genuinely needed.
- Package dependencies may be requested for build-time installation only after user approval. Good examples for web scraping are requests, beautifulsoup4, or feedparser when genuinely useful.
- Do not request email, calendar, finance, browser cookie, public posting, purchase, file deletion, or arbitrary shell behavior.

Choose all skill properties yourself based on the request:
- skill_name: safe snake_case, matching ^[a-zA-Z0-9_-]+$.
- display_name: human readable.
- skill_type.
- interface_type.
- input_schema and output_schema as JSON Schema objects when useful; otherwise null.
- tool_ui_schema for interface_type=tool; otherwise null.
- permissions, dependencies, risk level, expected input/output, files, and validation steps.
- If the user asks for recurring execution, include schedule as manifest intent. Otherwise set schedule to null.
- Supported schedule shapes:
  - {{"type": "daily", "time": "HH:MM", "timezone": "IANA timezone", "input": {{}}}}
  - {{"type": "weekly", "day": "weekday", "time": "HH:MM", "timezone": "IANA timezone", "input": {{}}}}
  - {{"type": "interval", "every": 1, "unit": "minutes|hours|days", "timezone": "IANA timezone", "input": {{}}}}

Return JSON with exactly this shape:
{{
  "goal": "string",
  "skill_name": "safe_name",
  "display_name": "Display Name",
  "skill_type": "instruction | automation",
  "interface_type": "chat | tool | hidden",
  "files_to_generate": ["manifest.json", "README.md"],
  "expected_input": {{}},
  "expected_output": {{}},
  "input_schema": null,
  "output_schema": null,
  "tool_ui_schema": null,
  "requested_permissions": {{
    "network": [],
    "filesystem_read": [],
    "filesystem_write": [],
    "secrets": [],
    "shell": false
  }},
  "requested_network_domains": [],
  "requested_dependencies": ["Python package names only, no URLs, no git refs, no local paths"],
  "schedule": null,
  "tests_required": false,
  "validation_steps": ["validate manifest.json", "inspect generated files"],
  "risk_level": "low | medium | high",
  "automatic_actions_blocked": [
    "installing the skill",
    "running the skill",
    "installing packages without approval"
  ]
}}

For a tool, use this tool_ui_schema shape:
{{
  "title": "Human tool title",
  "description": "One sentence explaining what this tool does.",
  "submit_label": "Run",
  "fields": [
    {{
      "name": "input_key",
      "label": "Input Label",
      "type": "text | number | textarea | checkbox | select",
      "placeholder": "Optional placeholder",
      "help_text": "Optional helper text",
      "required": true,
      "default": "",
      "options": ["Only for select fields"]
    }}
  ],
  "result_template": {{
    "primary_field": "result",
    "primary_label": "Result"
  }}
}}

User Project mode request:
{message}
""".strip()


def parse_json_object(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise SkillPlanError("Codex returned an unreadable skill generation plan") from exc
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested_exc:
            raise SkillPlanError("Codex returned malformed JSON for the skill generation plan") from nested_exc
    if not isinstance(data, dict):
        raise SkillPlanError("Codex skill generation plan must be a JSON object")
    return data


def _infer_skill_identity(message: str) -> dict[str, str]:
    lowered = message.lower()
    if "github" in lowered and "trending" in lowered:
        if "weekly" in lowered:
            return {
                "skill_name": "weekly_github_trending_insights",
                "display_name": "Weekly GitHub Trending Insights",
            }
        return {
            "skill_name": "github_trending_insights",
            "display_name": "GitHub Trending Insights",
        }

    words = re.findall(r"[a-z0-9]+", lowered)
    stopwords = {
        "a",
        "an",
        "and",
        "app",
        "automation",
        "build",
        "can",
        "create",
        "for",
        "from",
        "generate",
        "i",
        "in",
        "into",
        "make",
        "my",
        "of",
        "on",
        "project",
        "run",
        "runs",
        "skill",
        "that",
        "the",
        "to",
        "tool",
        "use",
        "with",
    }
    chosen: list[str] = []
    for word in words:
        if word in stopwords or len(word) < 3:
            continue
        if word not in chosen:
            chosen.append(word)
        if len(chosen) == 4:
            break
    if not chosen:
        chosen = ["generated", "skill"]
    safe_name = "_".join(chosen)[:80].strip("_") or "generated_skill"
    return {
        "skill_name": safe_name,
        "display_name": safe_name.replace("_", " ").title(),
    }


def _infer_schedule(message: str) -> dict[str, Any] | None:
    lowered = message.lower()
    if "weekly" in lowered:
        return {
            "type": "weekly",
            "day": "monday",
            "time": "09:00",
            "timezone": "America/Toronto",
            "input": {},
        }
    if "daily" in lowered:
        return {
            "type": "daily",
            "time": "09:00",
            "timezone": "America/Toronto",
            "input": {},
        }
    return None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
