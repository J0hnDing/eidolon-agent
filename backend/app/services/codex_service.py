import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Skill, SkillGenerationRequest
from app.services.manifest_validator import validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService


class CodexGenerationError(RuntimeError):
    pass


class CodexAdapter(Protocol):
    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        pass


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class RealCodexAdapter:
    def __init__(
        self,
        command: str | None = None,
        timeout_seconds: int | None = None,
        sandbox_mode: str | None = None,
        approval_policy: str | None = None,
        enable_search: str | None = None,
        model: str | None = None,
    ) -> None:
        self.command = command or os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
        self.timeout_seconds = timeout_seconds or _env_int("PERSONAL_AGENT_CODEX_TIMEOUT_SECONDS", 300)
        self.sandbox_mode = sandbox_mode or os.getenv("PERSONAL_AGENT_CODEX_SANDBOX", "workspace-write")
        self.approval_policy = approval_policy or os.getenv("PERSONAL_AGENT_CODEX_APPROVAL_POLICY", "never")
        self.enable_search = enable_search or os.getenv("PERSONAL_AGENT_CODEX_ENABLE_SEARCH", "auto")
        self.model = model if model is not None else os.getenv("PERSONAL_AGENT_CODEX_MODEL")

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = output_dir / "codex_prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = [
            self.command,
            "--ask-for-approval",
            self.approval_policy,
        ]
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
            ]
        )
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        return subprocess.run(
            command,
            cwd=output_dir,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            shell=False,
        )

    def _should_enable_search(self, plan: dict) -> bool:
        mode = self.enable_search.strip().lower()
        if mode in {"1", "true", "yes", "on"}:
            return True
        if mode in {"0", "false", "no", "off"}:
            return False
        return bool(plan.get("requested_network_domains") or plan.get("requested_dependencies"))


class FakeCodexAdapter:
    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        skill_type = plan["skill_type"]
        permissions = plan["requested_permissions"]
        manifest = {
            "name": plan["skill_name"],
            "description": plan["goal"],
            "skill_type": skill_type,
            "interface_type": plan.get("interface_type", "chat"),
            "entrypoint": "skill.py" if skill_type in {"automation", "hybrid"} else None,
            "instructions_path": "SKILL.md" if skill_type in {"instruction", "hybrid"} else None,
            "input_schema": plan.get("input_schema"),
            "output_schema": plan.get("output_schema"),
            "tool_ui_schema": plan.get("tool_ui_schema"),
            "risk_level": plan["risk_level"],
            "permissions": permissions,
            "schedule": None,
            "created_by": "codex",
            "enabled": False,
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (output_dir / "README.md").write_text(f"# {plan['display_name']}\n\n{plan['goal']}\n", encoding="utf-8")
        if skill_type in {"instruction", "hybrid"}:
            (output_dir / "SKILL.md").write_text(
                "# Instructions\n\nUse this reusable capability with care. Do not perform unsafe actions.\n",
                encoding="utf-8",
            )
        if skill_type in {"automation", "hybrid"}:
            (output_dir / "skill.py").write_text(
                "import json\n"
                "import sys\n\n"
                "def main():\n"
                "    payload = json.loads(sys.stdin.read() or '{}')\n"
                "    print(json.dumps({'title': 'Generated Proposed Skill', 'items': [], 'input': payload, 'warnings': []}))\n\n"
                "if __name__ == '__main__':\n"
                "    main()\n",
                encoding="utf-8",
            )
            tests_dir = output_dir / "tests"
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "test_skill.py").write_text(
                "import json\n"
                "import subprocess\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                "def test_generated_skill_outputs_json():\n"
                "    skill_path = Path(__file__).resolve().parents[1] / 'skill.py'\n"
                "    result = subprocess.run([sys.executable, str(skill_path)], input='{}', capture_output=True, text=True, timeout=5, shell=False)\n"
                "    assert result.returncode == 0\n"
                "    assert isinstance(json.loads(result.stdout), dict)\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args=["fake-codex"], returncode=0, stdout="fake generation complete", stderr="")


def default_codex_adapter() -> CodexAdapter:
    if should_use_real_codex():
        return RealCodexAdapter()
    return FakeCodexAdapter()


def should_use_real_codex() -> bool:
    mode = os.getenv("PERSONAL_AGENT_CODEX_MODE", "auto").strip().lower()
    if mode == "real":
        return True
    if mode in {"fake", "dev", "stub", "local"}:
        return False
    command = os.getenv("PERSONAL_AGENT_CODEX_COMMAND", "codex")
    return shutil.which(command) is not None


@dataclass
class CodexService:
    db: Session
    adapter: CodexAdapter | None = None
    project_root: Path | None = None

    def __post_init__(self) -> None:
        if self.project_root is None:
            self.project_root = Path(__file__).resolve().parents[3]
        self.project_root = self.project_root.resolve()
        if self.adapter is None:
            self.adapter = default_codex_adapter()
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)

    def generate_from_request(self, generation_request: SkillGenerationRequest) -> tuple[Skill, object]:
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
        installed_dir = self.proposed_service.installed_dir(skill_name)
        if installed_dir.exists():
            raise CodexGenerationError(f"Installed skill already exists: {skill_name}")
        if proposed_dir.exists():
            shutil.rmtree(proposed_dir)
        proposed_dir.mkdir(parents=True)

        generation_request.status = "generating"
        self.db.commit()

        prompt = self.build_prompt(plan, proposed_dir)
        result = self.adapter.generate(prompt, proposed_dir, plan)
        if result.returncode != 0:
            generation_request.status = "failed"
            generation_request.error_message = result.stderr or "Codex generation failed"
            self.db.commit()
            raise CodexGenerationError(generation_request.error_message)

        skill = self.create_or_update_skill_record(plan, proposed_dir)
        validation = self.proposed_service.validate_proposed_skill(skill)
        if validation.manifest_valid:
            self.update_skill_record_from_manifest(skill, proposed_dir)
        generation_request.status = "generated"
        generation_request.proposed_skill_id = skill.id
        if not validation.ok:
            generation_request.error_message = validation.error_message
        self.db.commit()
        if validation.manifest_valid:
            PermissionService(self.db, project_root=self.project_root).create_runtime_request(skill)
        self.db.refresh(generation_request)
        return skill, validation

    def repair_skill(self, skill: Skill, failure_context: dict) -> subprocess.CompletedProcess[str]:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        plan = self.plan_from_skill(skill, failure_context)
        prompt = self.build_repair_prompt(skill, skill_dir, failure_context)
        result = self.adapter.generate(prompt, skill_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex repair failed")
        return result

    def plan_from_skill(self, skill: Skill, failure_context: dict) -> dict:
        return {
            "goal": failure_context.get("user_request") or f"Repair skill {skill.name}.",
            "skill_name": skill.name,
            "display_name": skill.name.replace("_", " ").title(),
            "skill_type": skill.skill_type,
            "interface_type": skill.interface_type,
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "tool_ui_schema": skill.tool_ui_schema_json,
            "requested_permissions": failure_context.get(
                "requested_permissions",
                {
                    "network": [],
                    "filesystem_read": [],
                    "filesystem_write": ["./cache"] if skill.skill_type in {"automation", "hybrid"} else [],
                    "secrets": [],
                    "shell": False,
                },
            ),
            "requested_network_domains": [],
            "requested_dependencies": [],
            "risk_level": skill.risk_level,
        }

    def update_skill_record_from_manifest(self, skill: Skill, proposed_dir: Path) -> None:
        manifest = validate_manifest_file(proposed_dir / "manifest.json")
        skill.description = manifest.description
        skill.skill_type = manifest.skill_type
        skill.interface_type = manifest.interface_type
        skill.risk_level = manifest.risk_level
        skill.instructions_path = manifest.instructions_path
        skill.input_schema_json = manifest.input_schema
        skill.output_schema_json = manifest.output_schema
        skill.tool_ui_schema_json = manifest.tool_ui_schema
        skill.enabled = False
        self.db.commit()
        self.db.refresh(skill)

    def create_or_update_skill_record(self, plan: dict, proposed_dir: Path) -> Skill:
        skill = self.db.scalar(select(Skill).where(Skill.name == plan["skill_name"]))
        values = {
            "description": plan["goal"],
            "skill_type": plan["skill_type"],
            "interface_type": plan.get("interface_type", "chat"),
            "status": "proposed",
            "risk_level": plan["risk_level"],
            "manifest_path": self.relative_path(proposed_dir / "manifest.json"),
            "instructions_path": "SKILL.md" if plan["skill_type"] in {"instruction", "hybrid"} else None,
            "input_schema_json": plan.get("input_schema"),
            "output_schema_json": plan.get("output_schema"),
            "tool_ui_schema_json": plan.get("tool_ui_schema"),
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

    def build_prompt(self, plan: dict, output_dir: Path) -> str:
        return f"""
You are generating an application skill for the Local-First Self-Extending Personal AI Assistant.

Application skill definition:
- A skill is a reusable capability package.
- skill_type is one of instruction, automation, hybrid.
- Instruction skills contain reusable instructions only.
- Automation skills contain executable Python automation.
- Hybrid skills contain both instructions and executable Python automation.

Write files only inside this exact folder:
{output_dir}

Do not modify backend, frontend, tests outside this folder, project metadata, git files, or any app source code.
Do not install packages.
Do not run the generated skill.
Do not set shell=true.
Do not implement email sending, calendar modification, trading, purchases, public posting, browser cookie access, file deletion, or arbitrary shell execution.

Generation plan:
{json.dumps(plan, indent=2)}

Required files:
- manifest.json
- README.md
- SKILL.md for instruction or hybrid skills
- skill.py and tests/test_skill.py for automation or hybrid skills

Manifest requirements:
- Use the plan skill_name, skill_type, risk_level, and requested_permissions exactly.
- Use the plan interface_type exactly.
- Include input_schema and output_schema from the plan when present.
- Include tool_ui_schema from the plan when present.
- If interface_type is tool, prefer a clear declarative tool_ui_schema so the app can render a user-friendly form. Do not generate React, HTML, JavaScript, or frontend app code.
- Network permissions must be explicit domains only; no wildcard permissions.
- shell must be false.
- secrets must be [].
- schedule must be null.

Executable skill requirements:
- read JSON from stdin
- write JSON object to stdout
- handle errors by returning JSON where possible
- no side effects on import
- use a main guard
- tests must not require installing packages

Instruction skill requirements:
- no skill.py required
- include SKILL.md with reusable instructions
""".strip()

    def build_repair_prompt(self, skill: Skill, output_dir: Path, failure_context: dict) -> str:
        return f"""
You are BuilderAgent repairing an application skill for the Local-First Self-Extending Personal AI Assistant.

Skill:
- name: {skill.name}
- skill_type: {skill.skill_type}
- interface_type: {skill.interface_type}

Controlled skill folder:
{output_dir}

Rules:
- Write only inside the controlled skill folder.
- Do not modify backend, frontend, project metadata, git files, or app source code.
- Do not install packages.
- Do not run the skill task automatically.
- Do not set shell=true.
- Do not add secrets, broad filesystem access, unrestricted network access, email/calendar/finance actions, browser automation, purchases, public posting, or file deletion.
- Preserve manifest.json, README.md, and required skill files.
- Preserve or reduce permissions unless the failure cannot be fixed without a declared permission change.
- Automation and hybrid skills must keep tests/test_skill.py.
- Executable skills must read JSON from stdin and return JSON on stdout.

Failure context:
{json.dumps(failure_context, indent=2)}

Repair the current milestone using the provided Tester failure context so validation can be rerun. Return no prose; write files only.
""".strip()

    def relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.project_root).as_posix()
