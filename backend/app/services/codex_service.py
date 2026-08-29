import json
import os
import re
import subprocess
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Skill, SkillGenerationRequest, SkillVersion
from app.schemas.codex_routing import ResolvedInvocationSettings
from app.schemas.manifest import ManifestPermissions, manifest_permission_requests
from app.schemas.skill_codex import SkillCodexRequest
from app.services.codex_cli_service import codex_cli_service
from app.services.codex_invocation_recorder import CodexInvocationRecorder
from app.services.codex_output_schema import output_schema_for_action, write_temporary_output_schema
from app.services.codex_routing_service import CodexRoutingError, CodexRoutingService
from app.services.default_permissions import (
    agent_permission_bounds,
    planning_permission_policy,
)
from app.services.dependency_environment import build_dependency_environment
from app.services.function_catalog_service import FunctionCatalogError, FunctionCatalogService
from app.services.manifest_validator import classify_permission_risk, validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.product_manager_contract_service import (
    ProductManagerContractError,
    ProductManagerContractService,
)
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.skill_package_files import snapshot_skill_files
from app.workflows.common.prompts import build_product_manager_prompt as build_common_product_manager_prompt
from app.workflows.single_codex.prompts import build_prompt as build_single_codex_prompt
from app.workflows.task_dag.prompts import (
    build_builder_prompt as build_task_dag_builder_prompt,
)
from app.workflows.task_dag.prompts import (
    build_product_manager_prompt as build_task_dag_product_manager_prompt,
)
from app.workflows.task_dag.prompts import (
    build_repair_prompt as build_task_dag_repair_prompt,
)
from app.workflows.task_dag.prompts import (
    build_tester_prompt as build_task_dag_tester_prompt,
)


class CodexGenerationError(RuntimeError):
    pass


class CodexProcessRegistry:
    """Tracks Codex subprocesses owned by cancellable agent runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._cancelled_run_ids: set[int] = set()

    def register(self, agent_run_id: int, process: subprocess.Popen[str]) -> None:
        with self._lock:
            cancelled = agent_run_id in self._cancelled_run_ids
            if not cancelled:
                self._processes[agent_run_id] = process
        if cancelled:
            self._terminate(process)

    def unregister(self, agent_run_id: int, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._processes.get(agent_run_id) is process:
                self._processes.pop(agent_run_id, None)

    def cancel(self, agent_run_id: int) -> None:
        with self._lock:
            self._cancelled_run_ids.add(agent_run_id)
            process = self._processes.get(agent_run_id)
        if process is not None:
            self._terminate(process)

    def reset(self, agent_run_id: int) -> None:
        with self._lock:
            self._cancelled_run_ids.discard(agent_run_id)
            self._processes.pop(agent_run_id, None)

    def is_cancelled(self, agent_run_id: int | None) -> bool:
        if agent_run_id is None:
            return False
        with self._lock:
            return agent_run_id in self._cancelled_run_ids

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return


codex_process_registry = CodexProcessRegistry()


PRODUCT_MANAGER_SANDBOX = "read-only"
WRITABLE_SKILL_SANDBOX = "workspace-write"


def _web_app_smoke_test_source(expected_text: str | None = None) -> str:
    expected_assertion = f"    assert {expected_text!r} in body\n" if expected_text else ""
    return (
        "import asyncio\n"
        "import importlib\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "MANIFEST = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))\n"
        "MODULE_NAME, ATTRIBUTE = MANIFEST['entrypoint'].split(':', 1)\n"
        "sys.path.insert(0, str(ROOT))\n"
        "MODULE = importlib.import_module(MODULE_NAME)\n"
        "APP = getattr(MODULE, ATTRIBUTE)\n\n"
        "async def request_root():\n"
        "    messages = []\n"
        "    received = False\n"
        "    async def receive():\n"
        "        nonlocal received\n"
        "        if not received:\n"
        "            received = True\n"
        "            return {'type': 'http.request', 'body': b'', 'more_body': False}\n"
        "        return {'type': 'http.disconnect'}\n"
        "    async def send(message):\n"
        "        messages.append(message)\n"
        "    scope = {\n"
        "        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',\n"
        "        'method': 'GET', 'scheme': 'http', 'path': '/', 'raw_path': b'/',\n"
        "        'query_string': b'', 'headers': [], 'client': ('test', 1), 'server': ('test', 80),\n"
        "    }\n"
        "    await APP(scope, receive, send)\n"
        "    status = next(message['status'] for message in messages if message['type'] == 'http.response.start')\n"
        "    body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')\n"
        "    return status, body.decode('utf-8')\n\n"
        "def test_web_app_manifest_and_rendering_contract():\n"
        "    assert MANIFEST['runtime'] == 'web_app'\n"
        "    status, body = asyncio.run(request_root())\n"
        "    assert status == 200\n"
        "    assert '<html' in body.lower()\n"
        + expected_assertion
    )


class CodexAdapter(Protocol):
    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        pass


DEFAULT_CODEX_ACTION_TIMEOUT_SECONDS = 300

CODEX_ACTION_TIMEOUT_SECONDS = {
    "product_manager_plan_build": 180,
    "product_manager_write_task_dag": 180,
    "product_manager_repair_blueprint": 180,
    "product_manager_update_review": 180,
    "single_codex_build": 900,
    "skill_generation": 600,
    "skill_build_task": 600,
    "skill_repair": 600,
    "skill_update_repair": 600,
    "skill_update": 600,
    "tester_write_tests": 300,
    "skill_runtime_codex": 45,
    "atlas_knowledge_expand": 180,
    "atlas_knowledge_explain_expand": 180,
}


def codex_action_timeout_seconds(plan: dict[str, object]) -> int:
    action = str(plan.get("codex_task") or plan.get("action") or "")
    return CODEX_ACTION_TIMEOUT_SECONDS.get(action, DEFAULT_CODEX_ACTION_TIMEOUT_SECONDS)


class RealCodexAdapter:
    uses_codex_account_quota = True

    def __init__(
        self,
        command: str | None = None,
        timeout_seconds: int | None = None,
        sandbox_mode: str | None = None,
        approval_policy: str | None = None,
        enable_search: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        invocation_settings: ResolvedInvocationSettings | None = None,
    ) -> None:
        self.command = command or codex_cli_service.command()
        self.timeout_seconds = timeout_seconds
        self.sandbox_mode = sandbox_mode or os.getenv("PERSONAL_AGENT_CODEX_SANDBOX", "workspace-write")
        self.approval_policy = approval_policy or os.getenv("PERSONAL_AGENT_CODEX_APPROVAL_POLICY", "never")
        self.enable_search = enable_search or os.getenv("PERSONAL_AGENT_CODEX_ENABLE_SEARCH", "auto")
        self.model = model if model is not None else os.getenv("PERSONAL_AGENT_CODEX_MODEL")
        self.reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else os.getenv("PERSONAL_AGENT_CODEX_REASONING_EFFORT")
        )
        self.invocation_settings = invocation_settings

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = output_dir / "codex_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = [self.command, "--ask-for-approval", self.approval_policy]
        if self.reasoning_effort:
            command.extend(["--config", f"model_reasoning_effort={json.dumps(self.reasoning_effort)}"])
        last_message_path = output_dir / "codex_last_message.txt"
        last_message_path.unlink(missing_ok=True)
        if self._should_enable_search(plan):
            command.append("--search")
        command.extend(
            [
                "exec",
                "-C",
                str(output_dir),
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "--sandbox",
                self.sandbox_mode,
                "--json",
                "--output-last-message",
                str(last_message_path),
            ]
        )
        if self.model:
            command.extend(["--model", self.model])
        output_schema_path = write_temporary_output_schema(plan.get("codex_task"), output_dir)
        if output_schema_path is not None:
            command.extend(["--output-schema", str(output_schema_path)])
        command.append("-")
        timeout_seconds = self.timeout_seconds if self.timeout_seconds is not None else codex_action_timeout_seconds(plan)
        try:
            raw_agent_run_id = plan.get("_agent_run_id")
            agent_run_id = raw_agent_run_id if isinstance(raw_agent_run_id, int) else None
            if agent_run_id is None:
                result = subprocess.run(
                    command,
                    cwd=output_dir,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=build_dependency_environment(output_dir),
                    timeout=timeout_seconds,
                    shell=False,
                )
            else:
                result = self._run_cancellable(
                    command,
                    output_dir=output_dir,
                    prompt=prompt,
                    timeout_seconds=timeout_seconds,
                    agent_run_id=agent_run_id,
                )
        finally:
            if output_schema_path is not None:
                output_schema_path.unlink(missing_ok=True)
        events = self._json_events(result.stdout)
        result.codex_usage = self._usage_from_events(events)  # type: ignore[attr-defined]
        settings = self.invocation_settings
        result.codex_requested_model = settings.requested_model if settings else self.model  # type: ignore[attr-defined]
        result.codex_model = settings.effective_model if settings else self.model  # type: ignore[attr-defined]
        result.codex_requested_reasoning_effort = (  # type: ignore[attr-defined]
            settings.requested_reasoning_effort if settings else self.reasoning_effort
        )
        result.codex_reasoning_effort = (  # type: ignore[attr-defined]
            settings.effective_reasoning_effort if settings else self.reasoning_effort
        )
        result.codex_route_source = settings.route_source if settings else "legacy_default"  # type: ignore[attr-defined]
        result.codex_role = settings.role if settings else None  # type: ignore[attr-defined]
        result.codex_difficulty = settings.difficulty if settings else None  # type: ignore[attr-defined]
        result.codex_adapter = "codex_cli"  # type: ignore[attr-defined]
        result.codex_cli_path = self.command  # type: ignore[attr-defined]
        result.codex_cli_version = None  # type: ignore[attr-defined]
        result.codex_cli_source = None  # type: ignore[attr-defined]
        if last_message_path.is_file():
            result.stdout = last_message_path.read_text(encoding="utf-8", errors="replace")
        return result

    @staticmethod
    def _run_cancellable(
        command: list[str],
        *,
        output_dir: Path,
        prompt: str,
        timeout_seconds: int,
        agent_run_id: int,
    ) -> subprocess.CompletedProcess[str]:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=output_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=build_dependency_environment(output_dir),
            shell=False,
            creationflags=creationflags,
        )
        codex_process_registry.register(agent_run_id, process)
        deadline = time.monotonic() + timeout_seconds
        pending_input: str | None = prompt
        stdout = ""
        stderr = ""
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout, stderr=stderr)
                try:
                    stdout, stderr = process.communicate(input=pending_input, timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    if not codex_process_registry.is_cancelled(agent_run_id):
                        continue
                    if process.poll() is None:
                        process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                    raise CodexGenerationError("Agent run is cancelled")
            if codex_process_registry.is_cancelled(agent_run_id):
                raise CodexGenerationError("Agent run is cancelled")
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            codex_process_registry.unregister(agent_run_id, process)

    @staticmethod
    def _json_events(raw: str) -> list[dict]:
        events = []
        for line in (raw or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _usage_from_events(events: list[dict]) -> dict[str, int] | None:
        for event in reversed(events):
            usage = event.get("usage")
            if event.get("type") != "turn.completed" or not isinstance(usage, dict):
                continue
            normalized = {
                "input_tokens": int(usage.get("input_tokens", 0)),
                "cached_input_tokens": int(usage.get("cached_input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "reasoning_output_tokens": int(usage.get("reasoning_output_tokens", 0)),
            }
            normalized["total_tokens"] = int(
                usage.get("total_tokens", normalized["input_tokens"] + normalized["output_tokens"])
            )
            return normalized
        return None

    def _should_enable_search(self, plan: dict) -> bool:
        mode = self.enable_search.strip().lower()
        if mode in {"1", "true", "yes", "on"}:
            return True
        if mode in {"0", "false", "no", "off"}:
            return False
        permission_plan = plan.get("permission_plan")
        if isinstance(permission_plan, dict):
            build_time = permission_plan.get("build_time")
            if isinstance(build_time, dict) and "internet_research" in build_time:
                return bool(build_time.get("internet_research"))
        return bool(plan.get("requested_network_domains") or plan.get("requested_dependencies"))

    def with_sandbox(self, sandbox_mode: str) -> "RealCodexAdapter":
        return RealCodexAdapter(
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            sandbox_mode=sandbox_mode,
            approval_policy=self.approval_policy,
            enable_search=self.enable_search,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            invocation_settings=self.invocation_settings,
        )

    def with_invocation_settings(
        self,
        settings: ResolvedInvocationSettings,
        *,
        sandbox_mode: str,
    ) -> "RealCodexAdapter":
        return RealCodexAdapter(
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            sandbox_mode=sandbox_mode,
            approval_policy=self.approval_policy,
            enable_search=self.enable_search,
            model=settings.effective_model,
            reasoning_effort=settings.effective_reasoning_effort,
            invocation_settings=settings,
        )


class UnavailableCodexAdapter:
    """Fail-closed adapter used when local Codex execution is unavailable."""

    uses_codex_account_quota = False

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        raise CodexGenerationError(self.reason)


def default_codex_adapter() -> CodexAdapter:
    mode = os.getenv("PERSONAL_AGENT_CODEX_MODE", "auto").strip().lower()
    if mode in {"disabled", "off"}:
        return UnavailableCodexAdapter("Codex is disabled by PERSONAL_AGENT_CODEX_MODE.")
    if mode in {"fake", "dev", "stub", "local"}:
        return UnavailableCodexAdapter(
            "Production fake Codex modes were removed. Configure a compatible Codex CLI or disable Codex explicitly."
        )
    status = codex_cli_service.resolve()
    if status.available and status.compatible and status.resolved_path:
        return RealCodexAdapter(command=status.resolved_path)
    return UnavailableCodexAdapter(status.error or "A compatible Codex CLI is unavailable.")


def _fallback_skill_identity(message: str) -> dict[str, str]:
    words = re.findall(r"[a-z0-9]+", message.lower())
    stopwords = {
        "a",
        "an",
        "and",
        "app",
        "application",
        "build",
        "create",
        "for",
        "from",
        "i",
        "make",
        "my",
        "of",
        "on",
        "project",
        "skill",
        "that",
        "the",
        "to",
        "use",
        "web",
        "with",
    }
    selected: list[str] = []
    for word in words:
        if word in stopwords or len(word) < 3:
            continue
        if word not in selected:
            selected.append(word)
        if len(selected) == 4:
            break
    if not selected:
        selected = ["generated", "skill"]
    skill_name = "_".join(selected)[:80].strip("_") or "generated_skill"
    return {
        "skill_name": skill_name,
        "display_name": skill_name.replace("_", " ").title(),
    }


def _fallback_runtime(message: str) -> str:
    lowered = message.lower()
    web_markers = ("web app", "web application", "interactive dashboard", "browser application")
    if any(marker in lowered for marker in web_markers):
        return "web_app"
    service_markers = ("schedule", "scheduled", "recurring", "daily", "weekly", "every day")
    return "service" if any(marker in lowered for marker in service_markers) else "function"


def _fallback_schedule(message: str) -> dict[str, object] | None:
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
    return {
        "type": "daily",
        "time": "09:00",
        "timezone": "America/Toronto",
        "input": {},
    }


@dataclass
class CodexService:
    db: Session
    adapter: CodexAdapter | None = None
    project_root: Path | None = None
    agent_run_id: int | None = None
    def __post_init__(self) -> None:
        if self.project_root is None:
            self.project_root = Path(__file__).resolve().parents[3]
        self.project_root = self.project_root.resolve()
        self.instruction_dir = Path(__file__).resolve().parents[1] / "agent_instructions"
        if self.adapter is None:
            self.adapter = default_codex_adapter()
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)
        self.routing_service = CodexRoutingService(self.db)
        self.invocations = CodexInvocationRecorder(self.db)
        self.product_manager_contracts = ProductManagerContractService()

    def _generate_product_manager(
        self,
        prompt: str,
        payload: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        adapter = self.adapter
        if isinstance(adapter, RealCodexAdapter):
            adapter = self._routed_adapter(
                adapter,
                role="product_manager",
                action=str(payload.get("codex_task") or "product_manager"),
                plan=payload,
                sandbox_mode=PRODUCT_MANAGER_SANDBOX,
            )
        result = adapter.generate(prompt, self._product_manager_workspace(), self._adapter_payload(payload))
        self.invocations.record_build_result(
            result,
            payload,
            default_adapter_name=type(self.adapter).__name__,
            prompt=prompt,
        )
        if codex_process_registry.is_cancelled(self.agent_run_id):
            raise CodexGenerationError("Agent run is cancelled")
        return result

    def _generate_writable_skill(
        self,
        prompt: str,
        output_dir: Path,
        plan: dict,
    ) -> subprocess.CompletedProcess[str]:
        self._assert_writable_skill_workspace(output_dir)
        adapter = self.adapter
        if isinstance(adapter, RealCodexAdapter):
            task = str(plan.get("codex_task") or "skill_generation")
            role = "tester" if task.startswith("tester_") else "builder"
            routing_action = task
            if role == "tester" and plan.get("mode") == "update":
                routing_action = "tester_update"
            elif role == "tester" and plan.get("test_file") == "tests/test_final_e2e.py":
                routing_action = "tester_final_e2e"
            adapter = self._routed_adapter(
                adapter,
                role=role,
                action=routing_action,
                plan=plan,
                sandbox_mode=WRITABLE_SKILL_SANDBOX,
            )
        result = adapter.generate(prompt, output_dir, self._adapter_payload(plan))
        self.invocations.record_build_result(
            result,
            plan,
            default_adapter_name=type(self.adapter).__name__,
            prompt=prompt,
        )
        if codex_process_registry.is_cancelled(self.agent_run_id):
            raise CodexGenerationError("Agent run is cancelled")
        return result

    def bind_agent_run(self, agent_run_id: int) -> None:
        self.agent_run_id = agent_run_id

    def _adapter_payload(self, payload: dict[str, object]) -> dict[str, object]:
        if self.agent_run_id is None:
            return payload
        return {**payload, "_agent_run_id": self.agent_run_id}

    def _generate_builder_with_test_guard(
        self,
        prompt: str,
        output_dir: Path,
        plan: dict,
    ) -> subprocess.CompletedProcess[str]:
        before = self._test_source_snapshot(output_dir)
        result = self._generate_writable_skill(prompt, output_dir, plan)
        after = self._test_source_snapshot(output_dir)
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        if changed:
            self._restore_test_source_snapshot(output_dir, before, after)
            raise CodexGenerationError(f"Builder modified Tester-owned files: {', '.join(changed)}")
        return result

    @staticmethod
    def _test_source_snapshot(output_dir: Path) -> dict[str, bytes]:
        tests_dir = output_dir / "tests"
        if not tests_dir.exists():
            return {}
        return {
            path.relative_to(output_dir).as_posix(): path.read_bytes()
            for path in tests_dir.rglob("*.py")
            if path.is_file()
        }

    @staticmethod
    def _restore_test_source_snapshot(
        output_dir: Path,
        before: dict[str, bytes],
        after: dict[str, bytes],
    ) -> None:
        for relative_path in after.keys() - before.keys():
            (output_dir / relative_path).unlink(missing_ok=True)
        for relative_path, content in before.items():
            path = output_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def consume_invocation_usage(self) -> list[dict[str, object]]:
        return self.invocations.consume_build_usage()

    def consume_agent_transcripts(self) -> list[dict[str, str]]:
        return self.invocations.consume_build_transcripts()

    def _routed_adapter(
        self,
        adapter: RealCodexAdapter,
        *,
        role: str,
        action: str,
        plan: dict[str, object],
        sandbox_mode: str,
    ) -> RealCodexAdapter:
        try:
            settings = self.routing_service.resolve(
                role=role,
                action=action,
                difficulty=self._plan_difficulty(plan),
            )
        except CodexRoutingError as exc:
            raise CodexGenerationError(str(exc)) from exc
        return adapter.with_invocation_settings(settings, sandbox_mode=sandbox_mode)

    @staticmethod
    def _plan_difficulty(plan: dict[str, object]) -> str | None:
        for value in (plan.get("task_node"), plan.get("task_context")):
            if not isinstance(value, dict):
                continue
            task_node = value.get("task_node") if isinstance(value.get("task_node"), dict) else value
            difficulty = task_node.get("difficulty") if isinstance(task_node, dict) else None
            if difficulty in {"easy", "medium", "hard"}:
                return str(difficulty)
        return None

    def _assert_proposed_skill_workspace(self, output_dir: Path) -> Path:
        resolved = output_dir.resolve()
        proposed_root = (self.project_root / "skills" / "proposed").resolve()
        if resolved.parent != proposed_root:
            raise CodexGenerationError("Generated skill workspace must be one skill folder inside skills/proposed")
        return resolved

    def _assert_writable_skill_workspace(self, output_dir: Path) -> Path:
        resolved = output_dir.resolve()
        proposed_root = (self.project_root / "skills" / "proposed").resolve()
        installed_root = (self.project_root / "skills" / "installed").resolve()
        in_proposed_skill = resolved.parent == proposed_root
        in_installed_skill = resolved != installed_root and resolved.is_relative_to(installed_root)
        if not in_proposed_skill and not in_installed_skill:
            raise CodexGenerationError(
                "Codex write workspace must be a controlled skill folder under skills/proposed or skills/installed"
            )
        return resolved

    def product_manager_plan_build(
        self,
        generation_request: SkillGenerationRequest,
        intent_prompt: dict[str, object],
    ) -> dict[str, object]:
        plan = generation_request.plan_json
        function_catalog_index = FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).available_index()
        permission_policy = planning_permission_policy()
        payload = {
            "codex_task": "product_manager_plan_build",
            "user_message": generation_request.user_message,
            "intent_prompt": intent_prompt,
            "generation_plan": plan,
            "function_catalog_index": function_catalog_index,
            "permission_policy": permission_policy,
        }
        initial_prompt_payload: dict[str, object] = {
            "intent_prompt": intent_prompt,
            "function_catalog_index": function_catalog_index,
            "permission_policy": permission_policy,
        }
        result = self._generate_product_manager_plan_session(
            generation_request,
            self.build_product_manager_prompt("plan_build", initial_prompt_payload),
            payload,
        )
        parsed = self._decode_product_manager_session_blueprint(
            self._parse_product_manager_json_strict(result)
        )
        try:
            planned = self.product_manager_contracts.sanitize_plan_build(parsed, generation_request.plan_json)
        except ProductManagerContractError as exc:
            raise CodexGenerationError(str(exc)) from exc
        blueprint = planned.get("blueprint")
        if not isinstance(blueprint, dict):
            return planned
        try:
            blueprint["functions"] = FunctionCatalogService(
                self.db,
                project_root=self.project_root,
            ).validate_available_ids(blueprint.get("functions"))
        except FunctionCatalogError as exc:
            raise CodexGenerationError(str(exc)) from exc
        planned["blueprint"] = blueprint
        return planned

    def _generate_product_manager_plan_session(
        self,
        generation_request: SkillGenerationRequest,
        initial_prompt: str,
        payload: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        if not isinstance(self.adapter, RealCodexAdapter):
            return self._generate_product_manager(initial_prompt, payload)

        from app.services.product_manager_session_service import product_manager_session_service

        schema = self._product_manager_session_output_schema()
        if schema is None:
            raise CodexGenerationError("ProductManager planning output schema is unavailable")
        plan = dict(generation_request.plan_json or {})
        stored_route = plan.get("product_manager_session_route")
        if isinstance(stored_route, dict):
            settings = ResolvedInvocationSettings.model_validate(stored_route)
        else:
            try:
                settings = self.routing_service.resolve(
                    role="product_manager",
                    action="product_manager_plan_build",
                )
            except CodexRoutingError as exc:
                raise CodexGenerationError(str(exc)) from exc
            plan["product_manager_session_route"] = settings.model_dump(mode="json")

        thread_id = generation_request.product_manager_thread_id
        latest_reply = self._latest_project_user_reply(generation_request)
        session_schema_note = (
            "\n\nStructured-output transport note: keep blueprint as the structured object defined "
            "by the output schema. For a function or service blueprint, encode input_schema and "
            "output_schema as compact JSON strings. For a web_app blueprint, use null for both. "
            "When schedule is not null, encode only schedule.input as a compact JSON string."
        )
        turn_input = initial_prompt + session_schema_note
        try:
            if thread_id:
                product_manager_session_service.resume_thread(thread_id)
                turn_input = (
                    "The user answered your clarification:\n\n"
                    f"{latest_reply}\n\n"
                    "Reassess the request and return the same structured planning contract."
                )
            else:
                thread_id = product_manager_session_service.start_thread(
                    cwd=self._product_manager_workspace(),
                    model=settings.effective_model,
                    reasoning_effort=settings.effective_reasoning_effort,
                    sandbox=PRODUCT_MANAGER_SANDBOX,
                    approval_policy="never",
                )
                generation_request.product_manager_thread_id = thread_id
                generation_request.plan_json = plan
                self.db.commit()
            turn = product_manager_session_service.run_structured_turn(
                thread_id,
                turn_input,
                schema,
                timeout_seconds=codex_action_timeout_seconds(payload),
                model=settings.effective_model,
                reasoning_effort=settings.effective_reasoning_effort,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            if not thread_id or plan.get("product_manager_session_recovery_attempted"):
                raise CodexGenerationError(f"ProductManager session failed: {exc}") from exc
            plan["product_manager_session_recovery_attempted"] = True
            recovery_payload = {
                "intent_prompt": payload.get("intent_prompt", {}),
                "project_conversation": plan.get("project_conversation", []),
                "function_catalog_index": payload.get("function_catalog_index", []),
                "permission_policy": payload.get("permission_policy", {}),
            }
            recovery_prompt = (
                self.build_product_manager_prompt("plan_build", recovery_payload) + session_schema_note
            )
            try:
                replacement_thread_id = product_manager_session_service.start_thread(
                    cwd=self._product_manager_workspace(),
                    model=settings.effective_model,
                    reasoning_effort=settings.effective_reasoning_effort,
                    sandbox=PRODUCT_MANAGER_SANDBOX,
                    approval_policy="never",
                )
                generation_request.product_manager_thread_id = replacement_thread_id
                thread_id = replacement_thread_id
                generation_request.plan_json = plan
                self.db.commit()
                turn = product_manager_session_service.run_structured_turn(
                    thread_id,
                    recovery_prompt,
                    schema,
                    timeout_seconds=codex_action_timeout_seconds(payload),
                    model=settings.effective_model,
                    reasoning_effort=settings.effective_reasoning_effort,
                )
            except (OSError, RuntimeError, TimeoutError, ValueError) as recovery_exc:
                raise CodexGenerationError(
                    f"ProductManager session recovery failed: {recovery_exc}"
                ) from recovery_exc

        generation_request.plan_json = plan
        self.db.commit()
        result = subprocess.CompletedProcess(
            args=["codex", "app-server", "product-manager-plan"],
            returncode=0,
            stdout=turn.output_text,
            stderr="",
        )
        result.codex_usage = turn.usage  # type: ignore[attr-defined]
        result.codex_requested_model = settings.requested_model  # type: ignore[attr-defined]
        result.codex_model = turn.model or settings.effective_model  # type: ignore[attr-defined]
        result.codex_requested_reasoning_effort = settings.requested_reasoning_effort  # type: ignore[attr-defined]
        result.codex_reasoning_effort = turn.reasoning_effort or settings.effective_reasoning_effort  # type: ignore[attr-defined]
        result.codex_route_source = settings.route_source  # type: ignore[attr-defined]
        result.codex_role = settings.role  # type: ignore[attr-defined]
        result.codex_difficulty = None  # type: ignore[attr-defined]
        result.codex_adapter = "codex_app_server"  # type: ignore[attr-defined]
        result.codex_thread_id = turn.thread_id  # type: ignore[attr-defined]
        result.codex_turn_id = turn.turn_id  # type: ignore[attr-defined]
        self.invocations.record_build_result(
            result,
            payload,
            default_adapter_name="codex_app_server",
            prompt=turn_input,
        )
        return result

    @staticmethod
    def _product_manager_session_output_schema() -> dict[str, object] | None:
        """Return an App Server schema with only free-form JSON leaves string-encoded."""
        schema = output_schema_for_action("product_manager_plan_build")
        if schema is None:
            return None
        strict_schema = deepcopy(schema)
        properties = strict_schema.get("properties")
        if isinstance(properties, dict):
            blueprint_wrapper = properties.get("blueprint")
            if isinstance(blueprint_wrapper, dict):
                blueprint_variants = blueprint_wrapper.get("anyOf")
                if isinstance(blueprint_variants, list) and len(blueprint_variants) == 2:
                    blueprint_schema = blueprint_variants[1]
                    if isinstance(blueprint_schema, dict):
                        blueprint_properties = blueprint_schema.get("properties")
                        if isinstance(blueprint_properties, dict):
                            for field_name in ("input_schema", "output_schema"):
                                blueprint_properties[field_name] = {
                                    "type": ["string", "null"],
                                    "description": (
                                        f"{field_name} encoded as compact JSON for a function; "
                                        "null for a web app."
                                    ),
                                }
                            schedule = blueprint_properties.get("schedule")
                            if isinstance(schedule, dict):
                                for variant in schedule.get("anyOf", []):
                                    if not isinstance(variant, dict):
                                        continue
                                    schedule_properties = variant.get("properties")
                                    if isinstance(schedule_properties, dict) and "input" in schedule_properties:
                                        schedule_properties["input"] = {
                                            "type": "string",
                                            "description": "Schedule input encoded as compact JSON.",
                                        }
        CodexService._remove_unsupported_transport_keywords(strict_schema)
        return strict_schema

    @staticmethod
    def _decode_product_manager_session_blueprint(parsed: dict[str, object]) -> dict[str, object]:
        blueprint = parsed.get("blueprint")
        if not isinstance(blueprint, dict):
            return parsed
        for field_name in ("input_schema", "output_schema"):
            encoded = blueprint.get(field_name)
            if isinstance(encoded, str):
                blueprint[field_name] = CodexService._decode_json_object(
                    encoded,
                    f"ProductManager blueprint {field_name}",
                )
        schedule = blueprint.get("schedule")
        if isinstance(schedule, dict) and isinstance(schedule.get("input"), str):
            schedule["input"] = CodexService._decode_json_object(
                schedule["input"],
                "ProductManager blueprint schedule.input",
            )
        return parsed

    @staticmethod
    def _decode_json_object(value: str, label: str) -> dict[str, object]:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CodexGenerationError(f"{label} was not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise CodexGenerationError(f"{label} must decode to an object")
        return decoded

    @staticmethod
    def _remove_unsupported_transport_keywords(value: object) -> None:
        if isinstance(value, dict):
            value.pop("uniqueItems", None)
            for child in value.values():
                CodexService._remove_unsupported_transport_keywords(child)
        elif isinstance(value, list):
            for child in value:
                CodexService._remove_unsupported_transport_keywords(child)

    @staticmethod
    def _latest_project_user_reply(generation_request: SkillGenerationRequest) -> str:
        conversation = generation_request.plan_json.get("project_conversation", [])
        if isinstance(conversation, list):
            for item in reversed(conversation):
                if isinstance(item, dict) and item.get("role") == "user":
                    return str(item.get("content") or "")
        return generation_request.user_message

    def archive_product_manager_thread(self, generation_request: SkillGenerationRequest) -> str | None:
        thread_id = generation_request.product_manager_thread_id
        if not thread_id or not isinstance(self.adapter, RealCodexAdapter):
            return None
        from app.services.product_manager_session_service import product_manager_session_service

        try:
            product_manager_session_service.archive_thread(thread_id)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            return str(exc)[:500]
        return None

    def product_manager_write_task_dag(
        self,
        generation_request: SkillGenerationRequest,
        blueprint: dict[str, object],
        permission_bounds: dict[str, object],
        *,
        prompt_builder: Callable[[dict[str, object]], str] = build_task_dag_product_manager_prompt,
    ) -> dict[str, object]:
        selected_function_context = FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).context(blueprint.get("functions"))
        function_catalog_index = [
            {
                "id": entry["id"],
                "category": entry["category"],
                "title": entry["title"],
                "description": entry["description"],
                "risk_level": entry["risk_level"],
            }
            for entry in selected_function_context
        ]
        payload = {
            "codex_task": "product_manager_write_task_dag",
            "user_message": generation_request.user_message,
            "blueprint_json": blueprint,
            "permission_bounds": permission_bounds,
            "generation_plan": generation_request.plan_json,
            "function_catalog_index": function_catalog_index,
        }
        fallback = self._fallback_task_dag(generation_request, blueprint)
        prompt_payload = {
            "blueprint_json": blueprint,
            "permission_bounds": permission_bounds,
            "function_catalog_index": function_catalog_index,
        }
        result = self._generate_product_manager(
            prompt_builder(prompt_payload),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback={"task_dag": fallback})
        return self.product_manager_contracts.sanitize_task_dag(parsed.get("task_dag"), fallback, blueprint)

    def product_manager_repair_blueprint(self, skill: Skill, user_request: str | None) -> dict[str, object]:
        function_catalog_index = FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).available_index()
        payload = {
            "codex_task": "product_manager_repair_blueprint",
            "skill_name": skill.name,
            "runtime": skill.runtime,
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "functions": self._skill_function_ids(skill),
            "user_request": user_request or f"Repair skill {skill.name}.",
            "function_catalog_index": function_catalog_index,
        }
        fallback = self._fallback_repair_blueprint(skill, user_request)
        result = self._generate_product_manager(
            self.build_product_manager_prompt("repair_blueprint", payload),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback={"blueprint": fallback})
        blueprint = self.product_manager_contracts.sanitize_blueprint(parsed.get("blueprint"), fallback)
        blueprint["functions"] = FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).validate_available_ids(blueprint.get("functions"))
        return blueprint

    def product_manager_update_review(self, skill: Skill, suggestion: str) -> dict[str, object]:
        function_catalog_index = FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).available_index()
        permission_policy = planning_permission_policy()
        payload = {
            "codex_task": "product_manager_update_review",
            "skill_name": skill.name,
            "description": skill.description,
            "suggestion": suggestion,
            "runtime": skill.runtime,
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "functions": self._skill_function_ids(skill),
            "project_files": self._read_skill_files(skill),
            "function_catalog_index": function_catalog_index,
            "permission_policy": permission_policy,
        }
        fallback = self._fallback_update_review(skill, suggestion)
        result = self._generate_product_manager(
            self.build_product_manager_prompt("update_review", payload),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback=fallback)
        review = self.product_manager_contracts.sanitize_update_review(skill, suggestion, parsed, fallback)
        blueprint = review.get("blueprint")
        if isinstance(blueprint, dict):
            blueprint["functions"] = FunctionCatalogService(
                self.db,
                project_root=self.project_root,
            ).validate_available_ids(blueprint.get("functions"))
        return review

    def product_manager_summary(self, summary_type: str, context: dict[str, object], fallback_summary: str) -> str:
        return fallback_summary

    def skill_runtime_codex_call(
        self,
        skill: Skill,
        payload: SkillCodexRequest,
        *,
        internet_access: bool,
    ) -> dict[str, object]:
        workspace = self.project_root / "runtime" / "skill_codex" / f"skill_{skill.id}"
        plan = {
            "codex_task": "skill_runtime_codex",
            "skill_id": skill.id,
            "skill_name": skill.name,
            "model": payload.model,
            "permission_plan": {
                "build_time": {
                    "internet_research": internet_access,
                }
            },
            "requested_network_domains": ["runtime-approved-network"] if internet_access else [],
        }
        prompt = (
            "You are Codex responding to an installed local skill through the backend Skill Codex Call API.\n"
            "Return exactly one JSON object matching the supplied output schema and no prose.\n"
            "Do not perform shell actions, filesystem changes, browser automation, purchases, posting, or secrets access.\n\n"
            f"Skill: {skill.name}\n"
            f"Requested model: {payload.model or 'default'}\n"
            f"Internet access allowed: {internet_access}\n\n"
            f"Skill context:\n{json.dumps(payload.context, indent=2)}\n\n"
            f"Prompt:\n{payload.prompt}"
        )
        adapter = self.adapter
        if isinstance(adapter, RealCodexAdapter) and payload.model:
            adapter = RealCodexAdapter(
                command=adapter.command,
                timeout_seconds=adapter.timeout_seconds,
                sandbox_mode=os.getenv("PERSONAL_AGENT_CODEX_SKILL_SANDBOX", "read-only"),
                approval_policy=adapter.approval_policy,
                enable_search="true" if internet_access else "false",
                model=payload.model,
            )
        try:
            result = adapter.generate(prompt, workspace, plan)
        except Exception as exc:
            detail = self.invocations.error_detail(str(exc))
            invocation = self.invocations.failed_skill_runtime(
                plan,
                adapter=adapter,
                is_real_adapter=isinstance(adapter, RealCodexAdapter),
                cli_status=codex_cli_service.resolve() if isinstance(adapter, RealCodexAdapter) else None,
                error_type=type(exc).__name__,
                error_message=detail,
            )
            self.invocations.record_skill_runtime(skill.id, invocation)
            raise CodexGenerationError(detail) from exc
        if result.returncode != 0:
            detail = self.invocations.error_detail(result.stderr, returncode=result.returncode)
            invocation = self.invocations.failed_skill_runtime(
                plan,
                adapter=adapter,
                is_real_adapter=isinstance(adapter, RealCodexAdapter),
                cli_status=codex_cli_service.resolve() if isinstance(adapter, RealCodexAdapter) else None,
                error_type="CodexCliExitError",
                error_message=detail,
                returncode=result.returncode,
                stderr=result.stderr,
            )
            self.invocations.record_skill_runtime(skill.id, invocation)
            raise CodexGenerationError(detail)
        invocation = self.invocations.from_result(
            result,
            plan,
            default_adapter_name=type(self.adapter).__name__,
        )
        if invocation is not None:
            self.invocations.record_skill_runtime(skill.id, invocation)
        parsed = self._parse_product_manager_json(result, fallback={"response": result.stdout.strip(), "notes": []})
        response = parsed.get("response")
        return {
            "response": response.strip() if isinstance(response, str) and response.strip() else result.stdout.strip(),
            "model": payload.model,
            "internet_access": internet_access,
        }

    def generate_from_request(
        self,
        generation_request: SkillGenerationRequest,
        *,
        builder_writes_tests: bool = True,
        initial_skill_status: str = "proposed",
        task_context: dict[str, object] | None = None,
        create_runtime_request: bool = True,
        workspace_prepared: bool = False,
    ) -> tuple[Skill, object]:
        if generation_request.status != "approved":
            raise CodexGenerationError("Generation request is not approved for generation")
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_generate(
            generation_request
        )
        if not permission_decision.allowed:
            raise CodexGenerationError(permission_decision.reason)
        plan = generation_request.plan_json
        skill_name = self.proposed_service.validate_skill_name(plan["skill_name"])
        proposed_dir = self.proposed_service.proposed_dir(skill_name)
        self._assert_proposed_skill_workspace(proposed_dir)
        installed_dir = self.proposed_service.installed_dir(skill_name)
        if installed_dir.exists():
            raise CodexGenerationError(f"Installed skill already exists: {skill_name}")
        if workspace_prepared:
            if not proposed_dir.is_dir():
                raise CodexGenerationError("Backend-prepared generation workspace is missing")
        else:
            proposed_dir = self.proposed_service.prepare_generation_workspace(skill_name)
        self._write_manifest_skeleton(proposed_dir, plan, task_context)

        generation_request.status = "generating"
        self.db.commit()

        plan_for_adapter = {**plan, "builder_writes_tests": builder_writes_tests}
        if task_context is not None:
            plan_for_adapter["task_context"] = task_context
        prompt = self.build_prompt(plan_for_adapter, proposed_dir, builder_writes_tests=builder_writes_tests)
        generate = self._generate_writable_skill if builder_writes_tests else self._generate_builder_with_test_guard
        result = generate(prompt, proposed_dir, plan_for_adapter)
        if result.returncode != 0:
            generation_request.status = "failed"
            generation_request.error_message = result.stderr or "Codex generation failed"
            self.db.commit()
            raise CodexGenerationError(generation_request.error_message)
        self.finalize_manifest(proposed_dir, plan, task_context)

        skill = self.create_or_update_skill_record(plan, proposed_dir, status=initial_skill_status)
        validation = self.proposed_service.validate_proposed_skill(skill)
        if validation.manifest_valid:
            self.update_skill_record_from_manifest(skill, proposed_dir)
        generation_request.status = "generated"
        generation_request.proposed_skill_id = skill.id
        if not validation.ok:
            generation_request.error_message = validation.error_message
        self.db.commit()
        if create_runtime_request and validation.manifest_valid:
            PermissionService(self.db, project_root=self.project_root).create_runtime_request(skill)
        self.db.refresh(generation_request)
        return skill, validation

    def run_single_codex_build(
        self,
        generation_request: SkillGenerationRequest,
        blueprint: dict[str, object],
        permission_plan: dict[str, object],
        *,
        workspace_prepared: bool = False,
        prompt_builder: Callable[[dict[str, object], dict[str, object], Path], str] = build_single_codex_prompt,
    ) -> tuple[Skill, subprocess.CompletedProcess[str]]:
        if generation_request.status != "approved":
            raise CodexGenerationError("Generation request is not approved for generation")
        permission_decision = PermissionService(self.db, project_root=self.project_root).can_generate(generation_request)
        if not permission_decision.allowed:
            raise CodexGenerationError(permission_decision.reason)

        plan = generation_request.plan_json
        skill_name = self.proposed_service.validate_skill_name(plan["skill_name"])
        proposed_dir = self.proposed_service.proposed_dir(skill_name)
        self._assert_proposed_skill_workspace(proposed_dir)
        if self.proposed_service.installed_dir(skill_name).exists():
            raise CodexGenerationError(f"Installed skill already exists: {skill_name}")
        if workspace_prepared:
            if not proposed_dir.is_dir():
                raise CodexGenerationError("Backend-prepared generation workspace is missing")
        else:
            proposed_dir = self.proposed_service.prepare_generation_workspace(skill_name)
        workflow_context = {
            "blueprint_json": blueprint,
            "permission_plan": permission_plan,
        }
        self._write_manifest_skeleton(proposed_dir, plan, workflow_context)

        generation_request.status = "generating"
        self.db.commit()
        plan_for_adapter = {
            **plan,
            "codex_task": "single_codex_build",
            "builder_writes_tests": True,
            "blueprint_json": blueprint,
            "permission_plan": permission_plan,
            "permission_bounds": agent_permission_bounds(permission_plan),
        }
        builder_blueprint = {
            **blueprint,
            "function_context": FunctionCatalogService(
                self.db,
                project_root=self.project_root,
            ).context(blueprint.get("functions")),
        }
        result = self._generate_writable_skill(
            prompt_builder(builder_blueprint, agent_permission_bounds(permission_plan), proposed_dir),
            proposed_dir,
            plan_for_adapter,
        )
        if result.returncode != 0:
            generation_request.status = "failed"
            generation_request.error_message = result.stderr or "Single-Codex build failed"
            self.db.commit()
            raise CodexGenerationError(generation_request.error_message)

        self.finalize_manifest(proposed_dir, plan, workflow_context)
        skill = self.create_or_update_skill_record(plan, proposed_dir, status="building")
        generation_request.status = "generated"
        generation_request.proposed_skill_id = skill.id
        generation_request.error_message = None
        self.db.commit()
        self.db.refresh(generation_request)
        return skill, result

    def build_skill_task(
        self,
        skill: Skill,
        generation_request: SkillGenerationRequest,
        task_context: dict[str, object],
    ) -> tuple[subprocess.CompletedProcess[str], object]:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        self._assert_proposed_skill_workspace(skill_dir)
        plan = {
            **generation_request.plan_json,
            "codex_task": "skill_build_task",
            "builder_writes_tests": False,
            "task_context": task_context,
            "existing_files": self._read_files_from_dir(skill_dir),
        }
        prompt = self.build_prompt(plan, skill_dir, builder_writes_tests=False)
        result = self._generate_builder_with_test_guard(prompt, skill_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex milestone build failed")
        self.finalize_manifest(skill_dir, generation_request.plan_json, task_context)
        validation = self.proposed_service.validate_proposed_skill(skill)
        if validation.manifest_valid:
            self.update_skill_record_from_manifest(skill, skill_dir)
        return result, validation

    def repair_skill(
        self,
        skill: Skill,
        failure_context: dict,
        *,
        build_workflow: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        self._assert_proposed_skill_workspace(skill_dir)
        plan = {
            **self.plan_from_skill(skill, failure_context),
            "codex_task": "skill_repair",
            "builder_writes_tests": False,
            "task_context": failure_context,
        }
        prompt = (
            build_task_dag_repair_prompt(skill, skill_dir, failure_context)
            if build_workflow == "task_dag"
            else self.build_repair_prompt(skill, skill_dir, failure_context)
        )
        result = self._generate_builder_with_test_guard(prompt, skill_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex repair failed")
        self.finalize_manifest(skill_dir, plan, failure_context)
        return result

    def repair_skill_version(
        self,
        skill: Skill,
        version: SkillVersion,
        failure_context: dict,
    ) -> subprocess.CompletedProcess[str]:
        version_dir = (self.project_root / version.folder_path).resolve()
        installed_root = (self.project_root / "skills" / "installed").resolve()
        if not version_dir.is_relative_to(installed_root):
            raise CodexGenerationError("Version folder must stay inside skills/installed")
        plan = {
            **self.plan_from_skill(skill, failure_context),
            "codex_task": "skill_update_repair",
            "version_id": version.id,
            "builder_writes_tests": False,
            "permission_bounds": failure_context.get("permission_bounds", {}),
        }
        prompt = self.build_repair_prompt(skill, version_dir, failure_context)
        result = self._generate_builder_with_test_guard(prompt, version_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex draft-version repair failed")
        self.finalize_manifest(version_dir, plan, failure_context)
        return result

    def update_skill_version(
        self,
        skill: Skill,
        version: SkillVersion,
        suggestion: str,
        blueprint: dict[str, object],
        permission_bounds: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        version_dir = (self.project_root / version.folder_path).resolve()
        installed_root = (self.project_root / "skills" / "installed").resolve()
        if not version_dir.is_relative_to(installed_root):
            raise CodexGenerationError("Version folder must stay inside skills/installed")
        plan = {
            **self.plan_from_skill(skill, {"user_request": suggestion}),
            "codex_task": "skill_update",
            "suggestion": suggestion,
            "blueprint_json": blueprint,
            "project_files": self._read_files_from_dir(version_dir),
            "permission_bounds": permission_bounds,
        }
        prompt = self.build_update_prompt(skill, version, version_dir, suggestion, blueprint, permission_bounds)
        result = self._generate_builder_with_test_guard(prompt, version_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex update failed")
        self.finalize_manifest(
            version_dir,
            plan,
            {"blueprint_json": blueprint, "permission_plan": permission_bounds},
        )
        return result

    def write_tests_for_skill(
        self,
        skill: Skill,
        tester_context: dict,
        *,
        build_workflow: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        self._assert_proposed_skill_workspace(skill_dir)
        plan = {
            **self.plan_from_skill(skill, tester_context),
            "codex_task": "tester_write_tests",
            "task_node": tester_context.get("task_node", {}),
            "test_file": tester_context.get("test_file"),
            "acceptance_criteria": tester_context.get("acceptance_criteria", []),
            "interface_contracts": tester_context.get("interface_contracts", []),
            "workspace_paths": tester_context.get("workspace_paths", []),
            "permission_bounds": tester_context.get("permission_bounds", {}),
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            **({"blueprint_contract": tester_context["blueprint_contract"]} if "blueprint_contract" in tester_context else {}),
            **({"milestone": tester_context["milestone"]} if "milestone" in tester_context else {}),
        }
        prompt = (
            build_task_dag_tester_prompt(skill_dir, tester_context)
            if build_workflow == "task_dag"
            else self.build_tester_prompt(skill, skill_dir, tester_context)
        )
        result = self._generate_writable_skill(prompt, skill_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex tester failed to write tests")
        return result

    def write_tests_for_version(
        self,
        skill: Skill,
        version: SkillVersion,
        tester_context: dict,
    ) -> subprocess.CompletedProcess[str]:
        version_dir = (self.project_root / version.folder_path).resolve()
        installed_root = (self.project_root / "skills" / "installed").resolve()
        if not version_dir.is_relative_to(installed_root):
            raise CodexGenerationError("Version folder must stay inside skills/installed")
        plan = {
            **self.plan_from_skill(skill, tester_context),
            "codex_task": "tester_write_tests",
            "mode": "update",
            "version_id": version.id,
            "task_node": tester_context.get("task_node", {}),
            "test_file": tester_context.get("test_file"),
            "acceptance_criteria": tester_context.get("acceptance_criteria", []),
            "interface_artifacts": tester_context.get("interface_artifacts", []),
            "code_files": tester_context.get("code_files", {}),
            "permission_bounds": tester_context.get("permission_bounds", {}),
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            **({"blueprint_json": tester_context["blueprint_json"]} if "blueprint_json" in tester_context else {}),
            **({"permission_plan": tester_context["permission_plan"]} if "permission_plan" in tester_context else {}),
            **({"milestone": tester_context["milestone"]} if "milestone" in tester_context else {}),
        }
        prompt = self.build_tester_prompt(skill, version_dir, {**tester_context, "mode": "update"})
        result = self._generate_writable_skill(prompt, version_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex tester failed to write draft-version tests")
        return result

    def plan_from_skill(self, skill: Skill, failure_context: dict) -> dict:
        return {
            "goal": failure_context.get("user_request") or f"Repair skill {skill.name}.",
            "skill_name": skill.name,
            "display_name": skill.name.replace("_", " ").title(),
            "runtime": skill.runtime,
            "function_requirements": list(skill.function_requirements_json or []),
            "integration_requirements": list(skill.integration_requirements_json or []),
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "requested_permissions": failure_context.get(
                "requested_permissions",
                {
                    "network": [],
                    "filesystem_read": [],
                    "filesystem_write": ["./cache"],
                    "secrets": [],
                    "shell": False,
                },
            ),
            "requested_network_domains": [],
            "requested_dependencies": [],
            "risk_level": skill.risk_level,
        }

    def _read_skill_files(self, skill: Skill) -> dict[str, str]:
        try:
            skill_dir = self.proposed_service.skill_dir_for_record(skill)
        except (ProposedSkillError, ValueError, FileNotFoundError):
            return {}
        return self._read_files_from_dir(skill_dir)

    def _read_files_from_dir(self, root: Path) -> dict[str, str]:
        return snapshot_skill_files(root)

    def update_skill_record_from_manifest(self, skill: Skill, proposed_dir: Path) -> None:
        manifest = validate_manifest_file(proposed_dir / "manifest.json")
        skill.description = manifest.description
        skill.runtime = manifest.runtime
        skill.risk_level = classify_permission_risk(manifest.permissions, manifest.dependencies)
        skill.instructions_path = manifest.instructions_path
        skill.input_schema_json = manifest.input_schema
        skill.output_schema_json = manifest.output_schema
        skill.function_requirements_json = list(manifest.function_requirements)
        skill.integration_requirements_json = [
            item.model_dump(mode="json") for item in manifest.integration_requirements
        ]
        skill.enabled = False
        self.db.commit()
        self.db.refresh(skill)

    def create_or_update_skill_record(self, plan: dict, proposed_dir: Path, *, status: str = "proposed") -> Skill:
        skill = self.db.scalar(select(Skill).where(Skill.name == plan["skill_name"]))
        values = {
            "description": plan.get("description") or plan.get("goal") or plan["skill_name"],
            "runtime": plan.get("runtime", "function"),
            "status": status,
            "risk_level": plan["risk_level"],
            "manifest_path": self.relative_path(proposed_dir / "manifest.json"),
            "instructions_path": self._planned_instructions_path(plan),
            "input_schema_json": plan.get("input_schema"),
            "output_schema_json": plan.get("output_schema"),
            "function_requirements_json": list(plan.get("function_requirements", []) or []),
            "integration_requirements_json": list(plan.get("integration_requirements", []) or []),
            "installed_path": None,
            "enabled": False,
        }
        if skill is None:
            skill = Skill(name=plan["skill_name"], **values)
            self.db.add(skill)
        else:
            for key, value in values.items():
                setattr(skill, key, value)
        self.db.commit()
        self.db.refresh(skill)
        return skill

    def _write_manifest_skeleton(
        self,
        proposed_dir: Path,
        plan: dict,
        task_context: dict[str, object] | None,
    ) -> None:
        (proposed_dir / "tests").mkdir(exist_ok=True)
        manifest_path = proposed_dir / "manifest.json"
        if manifest_path.exists():
            return
        manifest_path.write_text(
            json.dumps(self._manifest_skeleton(plan, task_context), indent=2),
            encoding="utf-8",
        )

    def finalize_manifest(
        self,
        skill_dir: Path,
        plan: dict,
        task_context: dict[str, object] | None = None,
    ) -> None:
        manifest_path = skill_dir / "manifest.json"
        skeleton = self._manifest_skeleton(plan, task_context)
        if not manifest_path.exists():
            manifest = skeleton
        else:
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return
            if not isinstance(raw, dict):
                return
            manifest = dict(raw)
            for backend_owned_key in ("risk_level", "created_by", "enabled", "active_version", "installed"):
                manifest.pop(backend_owned_key, None)
            for key, value in skeleton.items():
                if key not in manifest:
                    manifest[key] = value
            permissions = manifest.get("permissions")
            if not isinstance(permissions, dict):
                manifest["permissions"] = skeleton["permissions"]
            else:
                try:
                    parsed_permissions = ManifestPermissions.model_validate(permissions)
                except ValueError:
                    # Preserve invalid declarations so authoritative manifest
                    # validation reports the exact generated contract error.
                    pass
                else:
                    manifest["permissions"] = manifest_permission_requests(parsed_permissions)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _manifest_skeleton(
        self,
        plan: dict,
        task_context: dict[str, object] | None,
    ) -> dict[str, object]:
        blueprint = self._context_blueprint(plan, task_context)
        permission_plan = self._context_permission_plan(plan, task_context)
        runtime_plan = permission_plan.get("runtime") if isinstance(permission_plan.get("runtime"), dict) else {}
        permissions = self._runtime_permissions(runtime_plan)
        sanitized_permission_plan = self.product_manager_contracts.sanitize_permission_plan(
            {"runtime": {**permissions, "dependencies": runtime_plan.get("dependencies", [])}},
            plan,
        )
        sanitized_permissions = self._manifest_permissions(sanitized_permission_plan["runtime"])
        runtime = str(blueprint.get("runtime") or plan.get("runtime") or "function")
        dependencies = list(
            runtime_plan.get("dependencies")
            or plan.get("requested_dependencies", [])
            or []
        )
        manifest = {
            "manifest_version": 1,
            "name": str(blueprint.get("name") or plan.get("skill_name")),
            "description": str(
                blueprint.get("description")
                or plan.get("description")
                or plan.get("goal")
                or plan.get("skill_name")
            ),
            "runtime": runtime,
            "entrypoint": "app:app" if runtime == "web_app" else "skill.py",
            "instructions_path": self._planned_instructions_path(plan),
            "input_schema": blueprint.get("input_schema", plan.get("input_schema")),
            "output_schema": blueprint.get("output_schema", plan.get("output_schema")),
            "function_requirements": list(plan.get("function_requirements", [])),
            "integration_requirements": list(plan.get("integration_requirements", [])),
            "dependencies": dependencies,
            "permissions": sanitized_permissions,
            "schedule": (
                None
                if runtime == "web_app"
                else blueprint.get("schedule") if isinstance(blueprint.get("schedule"), dict) else plan.get("schedule")
            ),
        }
        return manifest

    def _planned_instructions_path(self, plan: dict) -> str | None:
        files = plan.get("files_to_generate")
        if isinstance(files, list) and any(str(path).replace("\\", "/") == "SKILL.md" for path in files):
            return "SKILL.md"
        return None

    def _sanitize_codex_permissions(self, permissions: dict[str, object], network: list[str]) -> dict[str, bool]:
        raw_codex = permissions.get("codex")
        if not isinstance(raw_codex, dict):
            raw_codex = {}
        sanitized = {
            "call_response": bool(raw_codex.get("call_response", False)),
            "internet_access": bool(raw_codex.get("internet_access", bool(network))),
        }
        for key, value in raw_codex.items():
            if key not in sanitized:
                sanitized[str(key)] = bool(value)
        return sanitized

    def _runtime_permissions(self, runtime: dict[str, object]) -> dict[str, object]:
        return {
            "network": list(runtime.get("network", []) or []),
            "filesystem_read": list(runtime.get("filesystem_read", []) or []),
            "filesystem_write": list(runtime.get("filesystem_write", []) or []),
            "secrets": list(runtime.get("secrets", []) or []),
            "shell": bool(runtime.get("shell", False)),
            "codex": runtime.get("codex", {"call_response": False, "internet_access": False}),
        }

    @staticmethod
    def _manifest_permissions(runtime: dict[str, object]) -> dict[str, object]:
        """Serialize approval-gated runtime requests into manifest form."""

        requested: dict[str, object] = {}
        network = list(runtime.get("network", []) or [])
        if network:
            requested["network"] = network
        raw_codex = runtime.get("codex") if isinstance(runtime.get("codex"), dict) else {}
        codex = {
            key: True
            for key in ("call_response", "internet_access")
            if raw_codex.get(key) is True
        }
        if codex:
            requested["codex"] = codex
        return requested

    def _flat_runtime_permissions(self, plan: dict[str, object]) -> dict[str, object]:
        permissions = plan.get(
            "requested_permissions",
            {"network": [], "filesystem_read": [], "filesystem_write": [], "secrets": [], "shell": False},
        )
        if not isinstance(permissions, dict):
            permissions = {"network": [], "filesystem_read": [], "filesystem_write": [], "secrets": [], "shell": False}
        network = list(permissions.get("network", plan.get("requested_network_domains", [])) or [])
        if not network:
            network = list(plan.get("requested_network_domains", []) or [])
        return {
            "network": network,
            "filesystem_read": [
                path
                for path in list(permissions.get("filesystem_read", []) or [])
                if str(path).replace("\\", "/").removeprefix("./").rstrip("/") != "cache"
            ],
            "filesystem_write": [
                path
                for path in list(permissions.get("filesystem_write", []) or [])
                if str(path).replace("\\", "/").removeprefix("./").rstrip("/") != "cache"
            ],
            "secrets": list(permissions.get("secrets", []) or []),
            "shell": bool(permissions.get("shell", False)),
            "codex": self._sanitize_codex_permissions(permissions, network),
        }

    def _context_blueprint(
        self,
        plan: dict,
        task_context: dict[str, object] | None,
    ) -> dict[str, object]:
        if task_context and isinstance(task_context.get("blueprint_json"), dict):
            return dict(task_context["blueprint_json"])  # type: ignore[arg-type]
        return {
            "description": plan.get("description") or plan.get("goal"),
            "name": plan.get("skill_name"),
            "runtime": plan.get("runtime", "function"),
        }

    def _context_permission_plan(
        self,
        plan: dict,
        task_context: dict[str, object] | None,
    ) -> dict[str, object]:
        if task_context and isinstance(task_context.get("permission_plan"), dict):
            return dict(task_context["permission_plan"])  # type: ignore[arg-type]
        return self.product_manager_contracts.sanitize_permission_plan(None, plan)

    def build_prompt(self, plan: dict, output_dir: Path, *, builder_writes_tests: bool = True) -> str:
        return build_task_dag_builder_prompt(plan, output_dir, builder_writes_tests=builder_writes_tests)

    def build_repair_prompt(self, skill: Skill, output_dir: Path, failure_context: dict) -> str:
        instruction = self._instruction("builder/repair.md")
        return f"""
{instruction}

Skill:
- name: {skill.name}
- runtime: {skill.runtime}

Controlled skill folder:
{output_dir}

Failure context:
{json.dumps(failure_context, indent=2)}

Repair the current task node or final end-to-end failure using the provided Tester failure context so validation can be rerun.
""".strip()

    def build_tester_prompt(self, skill: Skill, output_dir: Path, tester_context: dict) -> str:
        instruction_name = "tester/update.md" if tester_context.get("mode") == "update" else "tester/repair.md"
        instruction = self._instruction(instruction_name)
        return f"""
{instruction}

Controlled skill folder:
{output_dir}

Tester context:
{json.dumps(tester_context, indent=2)}
""".strip()

    def build_update_prompt(
        self,
        skill: Skill,
        version: SkillVersion,
        output_dir: Path,
        suggestion: str,
        blueprint: dict[str, object],
        permission_bounds: dict[str, object],
    ) -> str:
        instruction = self._instruction("builder/update.md")
        function_context = FunctionCatalogService(
            self.db,
            project_root=self.project_root,
        ).context(blueprint.get("functions"))
        return f"""
{instruction}

Skill:
- name: {skill.name}
- runtime: {skill.runtime}

Draft version:
- version: {version.version}
- folder: {output_dir}

User improvement suggestion:
{suggestion}

ProductManager update blueprint:
{json.dumps(blueprint, indent=2)}

Permission bounds:
{json.dumps(permission_bounds, indent=2)}

Selected function context:
{json.dumps(function_context, indent=2)}

Current draft files:
{json.dumps(self._read_files_from_dir(output_dir), indent=2)}
""".strip()

    def build_product_manager_prompt(self, task: str, payload: dict[str, object]) -> str:
        if task == "plan_build":
            return build_common_product_manager_prompt(task, payload)
        instruction_by_task = {
            "repair_blueprint": "product_manager/repair.md",
            "update_review": "product_manager/update.md",
        }
        instruction_name = instruction_by_task.get(task)
        if instruction_name is None:
            raise CodexGenerationError(f"Unknown ProductManager prompt task: {task}")
        instruction = self._instruction(instruction_name)
        return f"""
{instruction}

Task: {task}

Payload:
{json.dumps(payload, indent=2)}
""".strip()

    def _parse_product_manager_json(self, result: subprocess.CompletedProcess[str], fallback: dict[str, object]) -> dict[str, object]:
        if result.returncode != 0:
            return fallback
        raw = (result.stdout or "").strip()
        if not raw:
            return fallback
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return fallback
            try:
                parsed = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return fallback
        return parsed if isinstance(parsed, dict) else fallback

    @staticmethod
    def _parse_product_manager_json_strict(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        if result.returncode != 0:
            raise CodexGenerationError(
                f"ProductManager planning failed with exit code {result.returncode}: {(result.stderr or '').strip()}"
            )
        raw = (result.stdout or "").strip()
        if not raw:
            raise CodexGenerationError("ProductManager planning returned no structured response")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexGenerationError("ProductManager planning returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise CodexGenerationError("ProductManager planning response must be a JSON object")
        return parsed

    def _product_manager_workspace(self) -> Path:
        workspace = self.project_root / "runtime" / "product_manager"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _fallback_build_blueprint(self, generation_request: SkillGenerationRequest) -> dict[str, object]:
        identity = _fallback_skill_identity(generation_request.user_message)
        runtime = _fallback_runtime(generation_request.user_message)
        return {
            "name": identity["skill_name"],
            "description": generation_request.user_message,
            "runtime": runtime,
            "input_schema": {"type": "object", "additionalProperties": True} if runtime in {"function", "service"} else None,
            "output_schema": {"type": "object", "additionalProperties": True} if runtime in {"function", "service"} else None,
            "expected_behavior": ["Implement the requested reusable capability."],
            "functions": [],
            "schedule": _fallback_schedule(generation_request.user_message) if runtime == "service" else None,
        }

    def _fallback_task_dag(
        self,
        generation_request: SkillGenerationRequest,
        blueprint: dict[str, object],
    ) -> dict[str, object]:
        runtime = str(blueprint.get("runtime") or "function")
        expected_files = ["manifest.json", "README.md", "app.py" if runtime == "web_app" else "skill.py"]
        if "manifest.json" not in expected_files:
            expected_files.insert(0, "manifest.json")
        node = {
            "id": "core_skill",
            "task_prompt": "Create the core proposed skill package.",
            "depends_on": [],
            "difficulty": "easy",
            "requires_tests": True,
            "parallel_safe": True,
            "write_paths": [path for path in expected_files if path != "manifest.json"],
            "acceptance_criteria": list(
                blueprint.get("expected_behavior")
                or [
                    "manifest.json is valid",
                    "required skill files exist",
                    "skill tests pass",
                    (
                        "web application exposes its declared ASGI entrypoint"
                        if runtime == "web_app"
                        else "function skill uses JSON stdin/stdout"
                    ),
                ]
            ),
            "test_expectations": ["validate manifest and generated skill behavior"],
            "function_ids": list(blueprint.get("functions", []) or []),
        }
        return {"schema_version": 1, "nodes": [node]}

    def _fallback_repair_blueprint(self, skill: Skill, user_request: str | None) -> dict[str, object]:
        return {
            "goal": user_request or f"Repair {skill.name}.",
            "skill_name": skill.name,
            "display_name": skill.name.replace("_", " ").replace("-", " ").title(),
            "runtime": skill.runtime,
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "functions": self._skill_function_ids(skill),
            "integration_scopes": self._skill_integration_scopes(skill),
            "milestones": [
                {
                    "name": "repair_skill",
                    "summary": "Repair the proposed skill package and confirm tests pass.",
                    "acceptance_criteria": ["manifest.json is valid", "tests pass", "permissions do not expand silently"],
                }
            ],
        }

    def _fallback_update_review(self, skill: Skill, suggestion: str) -> dict[str, object]:
        text = suggestion.strip()
        requested_network_domains: list[str] = []
        requested_dependencies: list[str] = []
        decision = {
            "decision": "build_next_milestone",
            "summary": f"Update {skill.name} with this improvement: {text}",
        }
        decision["blueprint"] = {
            "goal": decision["summary"],
            "skill_name": skill.name,
            "runtime": skill.runtime,
            "suggestion": suggestion,
            "functions": self._skill_function_ids(skill),
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "permission_plan": {
                "build_time": {
                    "internet_research": bool(requested_network_domains or requested_dependencies),
                    "dependencies": requested_dependencies,
                },
                "runtime": {
                    "network": requested_network_domains,
                    "filesystem_read": [],
                    "filesystem_write": ["./cache"] if requested_network_domains else [],
                    "secrets": [],
                    "shell": False,
                    "codex": {"call_response": False, "internet_access": bool(requested_network_domains)},
                    "dependencies": requested_dependencies,
                },
            },
            "requested_network_domains": requested_network_domains,
            "requested_dependencies": requested_dependencies,
            "milestones": [
                {
                    "name": "update_version",
                    "summary": "Copy the active version, implement the requested improvement, and validate the draft.",
                    "acceptance_criteria": [
                        "active version folder is not modified",
                        "draft version manifest is valid",
                        "draft version tests pass when executable",
                        "runtime permission changes are detected before activation",
                    ],
                }
            ],
        }
        return decision

    @staticmethod
    def _skill_function_ids(skill: Skill) -> list[str]:
        function_ids = [str(name) for name in skill.function_requirements_json or []]
        for requirement in skill.integration_requirements_json or []:
            if not isinstance(requirement, dict):
                continue
            function_ids.extend(str(operation) for operation in requirement.get("operations", []) or [])
        return list(dict.fromkeys(function_ids))

    @staticmethod
    def _skill_integration_scopes(skill: Skill) -> dict[str, object]:
        scopes: dict[str, object] = {}
        for requirement in skill.integration_requirements_json or []:
            if not isinstance(requirement, dict):
                continue
            provider = str(requirement.get("provider") or "").strip()
            resource_scope = requirement.get("resource_scope")
            if provider and isinstance(resource_scope, dict):
                scopes[provider] = resource_scope
        return scopes

    def relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.project_root).as_posix()

    def _instruction(self, relative_path: str) -> str:
        instruction_path = Path(relative_path)
        if instruction_path.is_absolute() or ".." in instruction_path.parts:
            raise CodexGenerationError(f"Invalid agent instruction path: {relative_path}")
        path = self.instruction_dir / instruction_path
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise CodexGenerationError(f"Missing agent instruction file: {relative_path}") from None
