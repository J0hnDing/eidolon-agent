import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.orm import Session

from app.schemas.codex_routing import ResolvedInvocationSettings
from app.schemas.common import RiskLevel, SkillRuntime
from app.schemas.manifest import ManifestFunctionRequirement, ManifestPermissions, classify_permission_risk
from app.services.codex_cli_service import codex_cli_service, should_use_real_codex
from app.services.codex_routing_service import CodexRoutingError, CodexRoutingService

SAFE_SKILL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


class SkillPlanError(ValueError):
    pass


class SkillGenerationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    skill_name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(min_length=1)
    runtime: SkillRuntime = "function"
    files_to_generate: list[str]
    expected_input: dict[str, Any]
    expected_output: dict[str, Any]
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    function_requirements: list[ManifestFunctionRequirement] = Field(default_factory=list)
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
        required_files = {"manifest.json", "skill.py" if self.runtime == "function" else "app.py"}
        missing = required_files - set(self.files_to_generate)
        if missing:
            raise ValueError(f"files_to_generate missing required files: {sorted(missing)}")
        if not any(path.startswith("tests/test_") and path.endswith(".py") for path in self.files_to_generate):
            raise ValueError("files_to_generate must include at least one Python test file")
        if self.runtime == "web_app" and self.schedule is not None:
            raise ValueError("web_app plans cannot use bounded-run schedules")
        if self.runtime == "function":
            if self.input_schema is None or self.output_schema is None:
                raise ValueError("function plans require explicit input_schema and output_schema contracts")
            if self.input_schema.get("type") != "object" or self.output_schema.get("type") != "object":
                raise ValueError("function input_schema and output_schema must declare type object")
        if not self.tests_required:
            raise ValueError("skill plans must require tests")
        if self.requested_network_domains != self.requested_permissions.network:
            raise ValueError("requested_network_domains must match requested_permissions.network")
        return self


class SkillPlanAdapter(Protocol):
    def build_plan(self, prompt: str, message: str) -> dict[str, Any]:
        pass


class FakeSkillPlanAdapter:
    def build_plan(self, prompt: str, message: str) -> dict[str, Any]:
        identity = _infer_skill_identity(message)
        runtime = _infer_runtime(message)
        schedule = _infer_schedule(message) if runtime == "function" else None
        implementation = "app.py" if runtime == "web_app" else "skill.py"
        test_file = "tests/test_app.py" if runtime == "web_app" else "tests/test_skill.py"
        return {
            "goal": message,
            "skill_name": identity["skill_name"],
            "display_name": identity["display_name"],
            "runtime": runtime,
            "files_to_generate": ["manifest.json", "README.md", implementation, test_file],
            "expected_input": {"input": "object"},
            "expected_output": {
                "title": "string",
                "items": [],
                "warnings": [],
                "schedule": schedule,
            },
            "input_schema": {"type": "object", "additionalProperties": True},
            "output_schema": {"type": "object", "additionalProperties": True},
            "function_requirements": [],
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
                "run skill tests",
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
        reasoning_effort: str | None = None,
    ) -> None:
        self.command = command or codex_cli_service.command()
        self.timeout_seconds = timeout_seconds or _env_int("PERSONAL_AGENT_CODEX_PLAN_TIMEOUT_SECONDS", 120)
        self.workdir = workdir or Path.cwd()
        self.sandbox_mode = sandbox_mode or os.getenv("PERSONAL_AGENT_CODEX_PLAN_SANDBOX", "read-only")
        self.approval_policy = approval_policy or os.getenv("PERSONAL_AGENT_CODEX_APPROVAL_POLICY", "never")
        self.model = model if model is not None else os.getenv("PERSONAL_AGENT_CODEX_MODEL")
        self.reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else os.getenv("PERSONAL_AGENT_CODEX_REASONING_EFFORT")
        )

    def build_plan(self, prompt: str, message: str) -> dict[str, Any]:
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
            raise SkillPlanError(result.stderr.strip() or "Codex could not build a skill generation plan")
        return parse_json_object(result.stdout)

    def with_invocation_settings(self, settings: ResolvedInvocationSettings) -> "RealSkillPlanAdapter":
        return RealSkillPlanAdapter(
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            workdir=self.workdir,
            sandbox_mode=self.sandbox_mode,
            approval_policy=self.approval_policy,
            model=settings.effective_model,
            reasoning_effort=settings.effective_reasoning_effort,
        )


def default_skill_plan_adapter() -> SkillPlanAdapter:
    if should_use_real_codex():
        return RealSkillPlanAdapter(workdir=Path(__file__).resolve().parents[3])
    return FakeSkillPlanAdapter()


@dataclass
class SkillPlanService:
    adapter: SkillPlanAdapter | None = None
    db: Session | None = None

    def __post_init__(self) -> None:
        if self.adapter is None:
            self.adapter = default_skill_plan_adapter()

    def build_generation_plan(self, message: str) -> dict[str, Any]:
        adapter = self.adapter
        if isinstance(adapter, RealSkillPlanAdapter) and self.db is not None:
            try:
                settings = CodexRoutingService(self.db).resolve(
                    role="product_manager", action="skill_plan"
                )
            except CodexRoutingError as exc:
                raise SkillPlanError(str(exc)) from exc
            adapter = adapter.with_invocation_settings(settings)
        raw_plan = adapter.build_plan(self.build_prompt(message), message)
        raw_plan.setdefault("goal", message)
        try:
            plan = SkillGenerationPlan.model_validate(raw_plan)
        except ValidationError as exc:
            raise SkillPlanError(str(exc)) from exc
        payload = plan.model_dump(mode="json")
        payload["risk_level"] = classify_permission_risk(plan.requested_permissions, plan.requested_dependencies)
        return payload

    def build_prompt(self, message: str) -> str:
        available_functions: list[dict[str, Any]] = []
        if self.db is not None:
            from app.services.function_registry_service import FunctionRegistryService

            available_functions = FunctionRegistryService(self.db).discovery_context()
        return f"""
You are designing an application skill generation plan for the Local-First Self-Extending Personal AI Assistant.

Return only one JSON object. Do not write files. Do not install packages. Do not run code.

Definitions:
- A skill is a reusable capability package.
- runtime=function is a bounded Python JSON stdin/stdout execution protocol.
- runtime=web_app is a persistent ASGI application protocol using an importable app:app-style entrypoint.
- Skills contain executable Python code and tests.
- Skills may include an optional SKILL.md for reusable instructions or operating notes.
- A web_app owns its HTML, CSS, JavaScript, rendering, interaction, state, and domain behavior inside its skill folder.
- A web_app must never edit or inject files into the Eidolon React frontend.
- Runtime alone determines interface exposure: web_app skills appear in Applications; function skills have no dedicated interface surface in this milestone.
- Installed function skills are discovered through a dynamic backend Function registry, never through the static trusted backend API catalog.
- A skill may use an installed function only when it declares that relationship in function_requirements with the exact function name and a clear reason.

Safety requirements:
- shell must be false.
- secrets must be [].
- skills must include at least one tests/test_*.py file; use tests/test_app.py for web applications and tests/test_skill.py for function skills.
- Use filesystem_write ["./cache"] only when useful; otherwise [].
- Use explicit network domains only when the user request genuinely needs future runtime network access.
- Do not request package dependencies unless genuinely needed.
- Package dependencies may be requested for build-time installation only after user approval. Good examples for web scraping are requests, beautifulsoup4, or feedparser when genuinely useful.
- Do not request email, calendar, finance, browser cookie, public posting, purchase, file deletion, or arbitrary shell behavior.

Choose all skill properties yourself based on the request:
- skill_name: safe snake_case, matching ^[a-zA-Z0-9_-]+$.
- display_name: human readable.
- runtime: choose web_app only when the requested capability needs a self-rendered interactive application; otherwise function.
- function skills require explicit object-shaped input_schema and output_schema JSON Schema contracts; web_app schemas may be null.
- function_requirements: select only installed registry functions genuinely needed by this skill. Do not invent function names.
- permissions, dependencies, risk level, expected input/output, files, and validation steps.
- If the user asks for recurring bounded function execution, include schedule as manifest intent. Always set schedule to null for web_app.
- Supported schedule shapes:
  - {{"type": "daily", "time": "HH:MM", "timezone": "IANA timezone", "input": {{}}}}
  - {{"type": "weekly", "day": "weekday", "time": "HH:MM", "timezone": "IANA timezone", "input": {{}}}}
  - {{"type": "interval", "every": 1, "unit": "minutes|hours|days", "timezone": "IANA timezone", "input": {{}}}}

Return JSON with exactly this shape:
{{
  "goal": "string",
  "skill_name": "safe_name",
  "display_name": "Display Name",
  "runtime": "function | web_app",
  "files_to_generate": ["manifest.json", "README.md"],
  "expected_input": {{}},
  "expected_output": {{}},
  "input_schema": {{"type": "object", "properties": {{}}, "additionalProperties": true}},
  "output_schema": {{"type": "object", "properties": {{}}, "additionalProperties": true}},
  "function_requirements": [
    {{"name": "installed_function_name", "reason": "why this skill needs this function"}}
  ],
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
  "tests_required": true,
  "validation_steps": ["validate manifest.json", "inspect generated files"],
  "risk_level": "low | medium | high",
  "automatic_actions_blocked": [
    "installing the skill",
    "running the skill",
    "installing packages without approval"
  ]
}}

Current dynamic Function registry (discovery does not grant invocation authority):
{json.dumps(available_functions, indent=2)}

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


def _infer_runtime(message: str) -> str:
    lowered = message.lower()
    web_app_markers = ("web app", "web application", "interactive dashboard", "browser application")
    return "web_app" if any(marker in lowered for marker in web_app_markers) else "function"


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
