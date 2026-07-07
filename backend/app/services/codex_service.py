import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Skill, SkillGenerationRequest, SkillVersion
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
        command = [self.command, "--ask-for-approval", self.approval_policy]
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
        task = plan.get("codex_task")
        if task == "product_manager_build_blueprint":
            blueprint = self._build_blueprint_from_plan(plan["generation_plan"], plan.get("user_message", ""))
            return subprocess.CompletedProcess(
                args=["fake-codex-product-manager"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "blueprint": blueprint,
                        "permission_plan": blueprint["permission_plan"],
                        "decision": "request_permission",
                        "summary": f"Build {blueprint['skill_name']} as a reusable skill.",
                    }
                ),
                stderr="",
            )
        if task == "product_manager_build_review":
            return subprocess.CompletedProcess(
                args=["fake-codex-product-manager-review"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "decision": "build_next_milestone",
                        "summary": "The request is reusable, bounded, and ready for blueprinting.",
                        "reason": "Fake Codex review accepts the project request for local tests.",
                        "user_prompt": None,
                        "optional_projects": [],
                    }
                ),
                stderr="",
            )
        if task == "product_manager_repair_blueprint":
            blueprint = {
                "goal": plan.get("user_request") or f"Repair {plan['skill_name']}.",
                "skill_name": plan["skill_name"],
                "skill_type": plan["skill_type"],
                "interface_type": plan.get("interface_type", "chat"),
                "expected_files": ["manifest.json", "README.md"],
                "milestones": [
                    {
                        "name": "repair_skill",
                        "summary": "Repair the proposed skill package and confirm tests pass.",
                        "acceptance_criteria": [
                            "manifest.json is valid",
                            "tests pass",
                            "permissions do not expand silently",
                        ],
                    }
                ],
            }
            return subprocess.CompletedProcess(
                args=["fake-codex-product-manager"],
                returncode=0,
                stdout=json.dumps({"blueprint": blueprint}),
                stderr="",
            )
        if task == "product_manager_update_review":
            decision = self._product_manager_update_decision(plan)
            return subprocess.CompletedProcess(
                args=["fake-codex-product-manager"],
                returncode=0,
                stdout=json.dumps(decision),
                stderr="",
            )
        if task == "product_manager_summary":
            return subprocess.CompletedProcess(
                args=["fake-codex-product-manager"],
                returncode=0,
                stdout=json.dumps({"summary": plan.get("fallback_summary", "ProductManager completed the review.")}),
                stderr="",
            )
        if task == "tester_write_tests":
            self._write_tester_tests(output_dir, plan)
            return subprocess.CompletedProcess(
                args=["fake-codex-tester"],
                returncode=0,
                stdout="fake tester wrote tests",
                stderr="",
            )
        if task == "skill_update":
            readme_path = output_dir / "README.md"
            existing = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else f"# {plan['skill_name']}\n"
            readme_path.write_text(
                existing.rstrip()
                + "\n\n## Proposed Update\n\n"
                + str(plan.get("suggestion", "Improve this skill.")).strip()
                + "\n",
                encoding="utf-8",
            )
            manifest_path = output_dir / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["description"] = manifest.get("description") or plan.get("goal") or plan["skill_name"]
                manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            return subprocess.CompletedProcess(
                args=["fake-codex-update"],
                returncode=0,
                stdout="fake update complete",
                stderr="",
            )
        if task == "skill_update_repair":
            return subprocess.CompletedProcess(
                args=["fake-codex-update-repair"],
                returncode=0,
                stdout="fake update repair complete",
                stderr="",
            )
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
            "dependencies": plan.get("requested_dependencies", []),
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
            if plan.get("builder_writes_tests", True):
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

    def _build_blueprint_from_plan(self, plan: dict, user_message: str) -> dict:
        acceptance_criteria = [
            "manifest.json is valid",
            "required skill files exist",
            "automation or hybrid tests pass",
            "executable skills use JSON stdin/stdout",
        ]
        if plan.get("interface_type") == "tool":
            acceptance_criteria.append("tool_ui_schema is present so the Tools page can render a user-friendly UI")
        permission_plan = {
            "build_time": {
                "codex_generation": True,
                "internet_research": bool(plan.get("requested_network_domains") or plan.get("requested_dependencies")),
                "dependencies": plan.get("requested_dependencies", []),
                "reason": "Codex needs to generate proposed skill files.",
            },
            "runtime": {
                "permissions": plan.get(
                    "requested_permissions",
                    {"network": [], "filesystem_read": [], "filesystem_write": [], "secrets": [], "shell": False},
                ),
                "network_domains": plan.get("requested_network_domains", []),
                "dependencies": plan.get("requested_dependencies", []),
                "reason": "Runtime permissions expected by the proposed skill design.",
            },
        }
        return {
            "goal": plan.get("goal") or user_message,
            "skill_name": plan.get("skill_name"),
            "skill_type": plan.get("skill_type"),
            "interface_type": plan.get("interface_type", "chat"),
            "expected_files": plan.get("files_to_generate", []),
            "expected_behavior": plan.get("expected_output", {}),
            "permission_plan": permission_plan,
            "milestones": [
                {
                    "name": "core_skill",
                    "summary": "Create the core proposed skill package.",
                    "acceptance_criteria": acceptance_criteria,
                }
            ],
        }

    def _product_manager_update_decision(self, plan: dict) -> dict:
        suggestion = str(plan.get("suggestion", "")).strip()
        requested_network_domains: list[str] = []
        requested_dependencies: list[str] = []
        decision = {
            "decision": "build_next_milestone",
            "summary": f"Update {plan['skill_name']} with this improvement: {suggestion}",
        }
        decision["blueprint"] = {
            "goal": decision["summary"],
            "skill_name": plan["skill_name"],
            "skill_type": plan["skill_type"],
            "interface_type": plan.get("interface_type", "chat"),
            "suggestion": suggestion,
            "permission_plan": {
                "build_time": {
                    "codex_generation": True,
                    "internet_research": bool(requested_network_domains or requested_dependencies),
                    "dependencies": requested_dependencies,
                    "reason": "Codex needs to create a draft version for this update.",
                },
                "runtime": {
                    "permissions": {
                        "network": requested_network_domains,
                        "filesystem_read": [],
                        "filesystem_write": ["./cache"] if requested_network_domains else [],
                        "secrets": [],
                        "shell": False,
                    },
                    "network_domains": requested_network_domains,
                    "dependencies": requested_dependencies,
                    "reason": "Expected runtime permissions for the updated skill design.",
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

    def _write_tester_tests(self, output_dir: Path, plan: dict) -> None:
        skill_type = plan.get("skill_type")
        if skill_type not in {"automation", "hybrid"}:
            return

        input_schema = plan.get("input_schema")
        output_schema = plan.get("output_schema")
        sample_input = self._sample_input_from_schema(input_schema)
        required_output_fields = []
        if isinstance(output_schema, dict) and isinstance(output_schema.get("required"), list):
            required_output_fields = [item for item in output_schema["required"] if isinstance(item, str)]

        tests_dir = output_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_skill.py").write_text(
            "import json\n"
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n\n"
            f"EXPECTED_NAME = {json.dumps(plan.get('skill_name'))}\n"
            f"EXPECTED_SKILL_TYPE = {json.dumps(skill_type)}\n"
            f"EXPECTED_INTERFACE_TYPE = {json.dumps(plan.get('interface_type', 'chat'))}\n"
            f"SAMPLE_INPUT_JSON = {json.dumps(json.dumps(sample_input))}\n"
            f"REQUIRED_OUTPUT_FIELDS = {json.dumps(required_output_fields)}\n\n"
            "ROOT = Path(__file__).resolve().parents[1]\n\n"
            "def run_skill(raw_input):\n"
            "    skill_path = ROOT / 'skill.py'\n"
            "    return subprocess.run(\n"
            "        [sys.executable, str(skill_path)],\n"
            "        input=raw_input,\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            "        timeout=5,\n"
            "        shell=False,\n"
            "    )\n\n"
            "def parse_stdout(stdout):\n"
            "    parsed = json.loads(stdout)\n"
            "    assert isinstance(parsed, dict)\n"
            "    return parsed\n\n"
            "def test_manifest_matches_blueprint_and_safe_contract():\n"
            "    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))\n"
            "    assert manifest['name'] == EXPECTED_NAME\n"
            "    assert manifest['skill_type'] == EXPECTED_SKILL_TYPE\n"
            "    assert manifest.get('interface_type', 'chat') == EXPECTED_INTERFACE_TYPE\n"
            "    assert manifest['permissions']['shell'] is False\n"
            "    assert manifest['permissions']['secrets'] == []\n"
            "    assert isinstance(manifest.get('dependencies', []), list)\n"
            "    if EXPECTED_INTERFACE_TYPE == 'tool':\n"
            "        assert isinstance(manifest.get('tool_ui_schema'), dict)\n"
            "        assert manifest['tool_ui_schema'].get('fields')\n\n"
            "def test_skill_accepts_representative_input_and_outputs_json_object():\n"
            "    result = run_skill(SAMPLE_INPUT_JSON)\n"
            "    assert result.returncode == 0, result.stderr\n"
            "    output = parse_stdout(result.stdout)\n"
            "    for field in REQUIRED_OUTPUT_FIELDS:\n"
            "        assert field in output\n\n"
            "def test_skill_handles_empty_input_without_traceback():\n"
            "    result = run_skill('{}')\n"
            "    assert result.returncode == 0, result.stderr\n"
            "    parse_stdout(result.stdout)\n",
            encoding="utf-8",
        )

    def _sample_input_from_schema(self, schema: object) -> dict:
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return {}
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        required = schema.get("required")
        field_names = required if isinstance(required, list) and required else list(properties.keys())
        sample = {}
        for raw_name in field_names:
            if not isinstance(raw_name, str):
                continue
            sample[raw_name] = self._sample_value_for_schema(properties.get(raw_name, {}))
        return sample

    def _sample_value_for_schema(self, schema: object) -> object:
        if not isinstance(schema, dict):
            return "sample"
        if "default" in schema:
            return schema["default"]
        enum_values = schema.get("enum")
        if isinstance(enum_values, list) and enum_values:
            return enum_values[0]
        schema_type = schema.get("type")
        if schema_type == "string":
            min_length = int(schema.get("minLength") or schema.get("min_length") or 1)
            max_length = schema.get("maxLength") or schema.get("max_length")
            value = "sample"
            if len(value) < min_length:
                value = "a" * min_length
            if isinstance(max_length, int) and len(value) > max_length:
                value = value[:max_length]
            return value
        if schema_type in {"integer", "number"}:
            return 1
        if schema_type == "boolean":
            return False
        if schema_type == "array":
            return []
        if schema_type == "object":
            return self._sample_input_from_schema(schema)
        return "sample"


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
        self.instruction_dir = Path(__file__).resolve().parents[1] / "agent_instructions"
        if self.adapter is None:
            self.adapter = default_codex_adapter()
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)

    def product_manager_build_blueprint(self, generation_request: SkillGenerationRequest) -> dict[str, object]:
        plan = generation_request.plan_json
        payload = {
            "codex_task": "product_manager_build_blueprint",
            "user_message": generation_request.user_message,
            "generation_plan": plan,
        }
        fallback = self._fallback_build_blueprint(generation_request)
        result = self.adapter.generate(
            self.build_product_manager_prompt("build_blueprint", payload),
            self._product_manager_workspace(),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback={"blueprint": fallback})
        blueprint = self._sanitize_blueprint(parsed.get("blueprint"), fallback)
        permission_plan = self._sanitize_permission_plan(parsed.get("permission_plan"), generation_request.plan_json)
        blueprint["permission_plan"] = permission_plan
        if isinstance(parsed.get("summary"), str):
            blueprint["product_manager_summary"] = str(parsed["summary"]).strip()
        if isinstance(parsed.get("decision"), str):
            blueprint["decision"] = str(parsed["decision"]).strip()
        return blueprint

    def product_manager_build_review(self, generation_request: SkillGenerationRequest) -> dict[str, object]:
        payload = {
            "codex_task": "product_manager_build_review",
            "user_message": generation_request.user_message,
            "project_conversation": generation_request.plan_json.get("project_conversation", []),
            "pending_user_prompt": generation_request.plan_json.get("pending_user_prompt"),
        }
        fallback = {
            "decision": "build_next_milestone",
            "summary": "The request is reusable, bounded, and ready for blueprinting.",
            "reason": "The request appears plausible for a local application skill.",
            "user_prompt": None,
            "optional_projects": [],
        }
        result = self.adapter.generate(
            self.build_product_manager_prompt("build_review", payload),
            self._product_manager_workspace(),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback=fallback)
        return self._sanitize_build_review(parsed, fallback)

    def product_manager_repair_blueprint(self, skill: Skill, user_request: str | None) -> dict[str, object]:
        payload = {
            "codex_task": "product_manager_repair_blueprint",
            "skill_name": skill.name,
            "skill_type": skill.skill_type,
            "interface_type": skill.interface_type,
            "user_request": user_request or f"Repair skill {skill.name}.",
        }
        fallback = self._fallback_repair_blueprint(skill, user_request)
        result = self.adapter.generate(
            self.build_product_manager_prompt("repair_blueprint", payload),
            self._product_manager_workspace(),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback={"blueprint": fallback})
        return self._sanitize_blueprint(parsed.get("blueprint"), fallback)

    def product_manager_update_review(self, skill: Skill, suggestion: str) -> dict[str, object]:
        payload = {
            "codex_task": "product_manager_update_review",
            "skill_name": skill.name,
            "skill_type": skill.skill_type,
            "interface_type": skill.interface_type,
            "description": skill.description,
            "suggestion": suggestion,
            "project_files": self._read_skill_files(skill),
        }
        fallback = self._fallback_update_review(skill, suggestion)
        result = self.adapter.generate(
            self.build_product_manager_prompt("update_review", payload),
            self._product_manager_workspace(),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback=fallback)
        return self._sanitize_update_review(skill, suggestion, parsed, fallback)

    def product_manager_summary(self, summary_type: str, context: dict[str, object], fallback_summary: str) -> str:
        payload = {
            "codex_task": "product_manager_summary",
            "summary_type": summary_type,
            "context": context,
            "fallback_summary": fallback_summary,
        }
        result = self.adapter.generate(
            self.build_product_manager_prompt("summary", payload),
            self._product_manager_workspace(),
            payload,
        )
        parsed = self._parse_product_manager_json(result, fallback={"summary": fallback_summary})
        summary = parsed.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
        return fallback_summary

    def generate_from_request(
        self,
        generation_request: SkillGenerationRequest,
        *,
        builder_writes_tests: bool = True,
        initial_skill_status: str = "proposed",
        milestone_context: dict[str, object] | None = None,
        create_runtime_request: bool = True,
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
        installed_dir = self.proposed_service.installed_dir(skill_name)
        if installed_dir.exists():
            raise CodexGenerationError(f"Installed skill already exists: {skill_name}")
        if proposed_dir.exists():
            shutil.rmtree(proposed_dir)
        proposed_dir.mkdir(parents=True)

        generation_request.status = "generating"
        self.db.commit()

        plan_for_adapter = {**plan, "builder_writes_tests": builder_writes_tests}
        if milestone_context is not None:
            plan_for_adapter["current_milestone"] = milestone_context
        prompt = self.build_prompt(plan_for_adapter, proposed_dir, builder_writes_tests=builder_writes_tests)
        result = self.adapter.generate(prompt, proposed_dir, plan_for_adapter)
        if result.returncode != 0:
            generation_request.status = "failed"
            generation_request.error_message = result.stderr or "Codex generation failed"
            self.db.commit()
            raise CodexGenerationError(generation_request.error_message)

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

    def build_skill_milestone(
        self,
        skill: Skill,
        generation_request: SkillGenerationRequest,
        milestone_context: dict[str, object],
    ) -> tuple[subprocess.CompletedProcess[str], object]:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        plan = {
            **generation_request.plan_json,
            "codex_task": "skill_build_milestone",
            "builder_writes_tests": False,
            "current_milestone": milestone_context,
            "existing_files": self._read_files_from_dir(skill_dir),
        }
        prompt = self.build_prompt(plan, skill_dir, builder_writes_tests=False)
        result = self.adapter.generate(prompt, skill_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex milestone build failed")
        validation = self.proposed_service.validate_proposed_skill(skill)
        if validation.manifest_valid:
            self.update_skill_record_from_manifest(skill, skill_dir)
        return result, validation

    def repair_skill(self, skill: Skill, failure_context: dict) -> subprocess.CompletedProcess[str]:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        plan = {**self.plan_from_skill(skill, failure_context), "builder_writes_tests": False}
        prompt = self.build_repair_prompt(skill, skill_dir, failure_context)
        result = self.adapter.generate(prompt, skill_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex repair failed")
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
        }
        prompt = self.build_repair_prompt(skill, version_dir, failure_context)
        result = self.adapter.generate(prompt, version_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex draft-version repair failed")
        return result

    def update_skill_version(
        self,
        skill: Skill,
        version: SkillVersion,
        suggestion: str,
        blueprint: dict[str, object],
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
        }
        prompt = self.build_update_prompt(skill, version, version_dir, suggestion, blueprint)
        result = self.adapter.generate(prompt, version_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex update failed")
        return result

    def write_tests_for_skill(self, skill: Skill, tester_context: dict) -> subprocess.CompletedProcess[str]:
        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        plan = {
            **self.plan_from_skill(skill, tester_context),
            "codex_task": "tester_write_tests",
            "blueprint_json": tester_context.get("blueprint_json", {}),
            "milestone": tester_context.get("milestone", {}),
            "code_files": tester_context.get("code_files", {}),
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "tool_ui_schema": skill.tool_ui_schema_json,
        }
        prompt = self.build_tester_prompt(skill, skill_dir, tester_context)
        result = self.adapter.generate(prompt, skill_dir, plan)
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
            "blueprint_json": tester_context.get("blueprint_json", {}),
            "milestone": tester_context.get("milestone", {}),
            "code_files": tester_context.get("code_files", {}),
            "input_schema": skill.input_schema_json,
            "output_schema": skill.output_schema_json,
            "tool_ui_schema": skill.tool_ui_schema_json,
        }
        prompt = self.build_tester_prompt(skill, version_dir, {**tester_context, "mode": "update"})
        result = self.adapter.generate(prompt, version_dir, plan)
        if result.returncode != 0:
            raise CodexGenerationError(result.stderr or "Codex tester failed to write draft-version tests")
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

    def _read_skill_files(self, skill: Skill) -> dict[str, str]:
        try:
            skill_dir = self.proposed_service.skill_dir_for_record(skill)
        except (ProposedSkillError, ValueError, FileNotFoundError):
            return {}
        return self._read_files_from_dir(skill_dir)

    def _read_files_from_dir(self, root: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        for relative_path in ("manifest.json", "README.md", "SKILL.md", "skill.py", "tests/test_skill.py"):
            path = root / relative_path
            if path.is_file():
                files[relative_path] = path.read_text(encoding="utf-8")[:12000]
        return files

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

    def create_or_update_skill_record(self, plan: dict, proposed_dir: Path, *, status: str = "proposed") -> Skill:
        skill = self.db.scalar(select(Skill).where(Skill.name == plan["skill_name"]))
        values = {
            "description": plan["goal"],
            "skill_type": plan["skill_type"],
            "interface_type": plan.get("interface_type", "chat"),
            "status": status,
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

    def build_prompt(self, plan: dict, output_dir: Path, *, builder_writes_tests: bool = True) -> str:
        instruction = self._instruction("builder_instruction_build.md")
        test_requirement = (
            "- tests must not require installing packages"
            if builder_writes_tests
            else "- do not create, modify, or delete tests. TesterAgent will inspect the implementation and write tests separately."
        )
        return f"""
{instruction}

Application skill definition:
- A skill is a reusable capability package.
- skill_type is one of instruction, automation, hybrid.
- Instruction skills contain reusable instructions only.
- Automation skills contain executable Python automation.
- Hybrid skills contain both instructions and executable Python automation.
- interface_type is one of chat, tool, hidden.
- Chat skills are primarily used through chat.
- Tool skills are installed enabled automation/hybrid skills exposed as manual forms in the Tools UI.
- Hidden skills are not shown as a normal user-facing entry point.
- Tool UI work is declarative manifest work: tool_ui_schema, input/output schemas, labels, fields, options, and result rendering hints. Do not generate frontend app code.

Write files only inside this exact folder:
{output_dir}

Generation plan:
{json.dumps(plan, indent=2)}

Product structure:
- Follow the ProductManager blueprint, expected_files, and current milestone.
- Do not write blueprint.json, permissions.json, milestone JSON files, or runtime/agent_runs artifacts; those are platform workflow artifacts.
- Platform validation still requires manifest.json and README.md in every skill package.
- Instruction and hybrid skills need the manifest instructions_path to point to an instructions file such as SKILL.md.
- Automation and hybrid skills need the manifest entrypoint to point to executable Python code such as skill.py.
- TesterAgent owns test files in this workflow unless builder_writes_tests is explicitly true.

Manifest requirements:
- Use the plan skill_name, skill_type, risk_level, and requested_permissions exactly.
- Use the plan interface_type exactly.
- Include dependencies from requested_dependencies exactly. Use [] when no packages are needed.
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
{test_requirement}

Instruction skill requirements:
- no skill.py required
- include SKILL.md with reusable instructions
""".strip()

    def build_repair_prompt(self, skill: Skill, output_dir: Path, failure_context: dict) -> str:
        instruction = self._instruction("builder_instruction_repair.md")
        return f"""
{instruction}

Skill:
- name: {skill.name}
- skill_type: {skill.skill_type}
- interface_type: {skill.interface_type}

Controlled skill folder:
{output_dir}

Failure context:
{json.dumps(failure_context, indent=2)}

Repair the current milestone using the provided Tester failure context so validation can be rerun.
""".strip()

    def build_tester_prompt(self, skill: Skill, output_dir: Path, tester_context: dict) -> str:
        instruction_name = "tester_instruction_update.md" if tester_context.get("mode") == "update" else "tester_instruction_build.md"
        instruction = self._instruction(instruction_name)
        return f"""
{instruction}

Controlled skill folder:
{output_dir}

Skill:
- name: {skill.name}
- skill_type: {skill.skill_type}
- interface_type: {skill.interface_type}

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
    ) -> str:
        instruction = self._instruction("builder_instruction_update.md")
        return f"""
{instruction}

Skill:
- name: {skill.name}
- skill_type: {skill.skill_type}
- interface_type: {skill.interface_type}

Draft version:
- version: {version.version}
- folder: {output_dir}

User improvement suggestion:
{suggestion}

ProductManager update blueprint:
{json.dumps(blueprint, indent=2)}

Current draft files:
{json.dumps(self._read_files_from_dir(output_dir), indent=2)}
""".strip()

    def build_product_manager_prompt(self, task: str, payload: dict[str, object]) -> str:
        instruction_by_task = {
            "build_blueprint": "product_manager_build.md",
            "build_review": "product_manager_plausibility_review.md",
            "repair_blueprint": "product_manager_repair.md",
            "update_review": "product_manager_update.md",
            "summary": "product_manager_summary.md",
        }
        instruction = self._instruction(instruction_by_task.get(task, "product_manager_summary.md"))
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

    def _sanitize_blueprint(self, value: object, fallback: dict[str, object]) -> dict[str, object]:
        if not isinstance(value, dict):
            return fallback
        blueprint = dict(value)
        for key in ("goal", "skill_name", "skill_type"):
            if not blueprint.get(key):
                blueprint[key] = fallback.get(key)
        blueprint["interface_type"] = blueprint.get("interface_type") or fallback.get("interface_type", "chat")
        milestones = blueprint.get("milestones")
        if not isinstance(milestones, list) or not milestones:
            blueprint["milestones"] = fallback.get("milestones", [])
        else:
            sanitized_milestones = []
            for milestone in milestones:
                if not isinstance(milestone, dict):
                    continue
                criteria = milestone.get("acceptance_criteria")
                sanitized_milestones.append(
                    {
                        "name": str(milestone.get("name") or "core_skill"),
                        "summary": str(milestone.get("summary") or "Build and validate the current milestone."),
                        "acceptance_criteria": criteria if isinstance(criteria, list) else [],
                    }
                )
            blueprint["milestones"] = sanitized_milestones or fallback.get("milestones", [])
        if blueprint.get("interface_type") == "tool":
            criteria = blueprint["milestones"][0].setdefault("acceptance_criteria", [])
            if "tool_ui_schema is present so the Tools page can render a user-friendly UI" not in criteria:
                criteria.append("tool_ui_schema is present so the Tools page can render a user-friendly UI")
        permission_source: object = blueprint.get("permission_plan")
        if not isinstance(permission_source, dict) and any(
            key in blueprint for key in ("requested_permissions", "requested_network_domains", "requested_dependencies")
        ):
            permission_source = {
                "build_time": {
                    "codex_generation": True,
                    "internet_research": bool(
                        blueprint.get("requested_network_domains") or blueprint.get("requested_dependencies")
                    ),
                    "dependencies": blueprint.get("requested_dependencies", []),
                    "reason": "Codex needs to generate controlled skill files.",
                },
                "runtime": {
                    "permissions": blueprint.get(
                        "requested_permissions",
                        {"network": [], "filesystem_read": [], "filesystem_write": [], "secrets": [], "shell": False},
                    ),
                    "network_domains": blueprint.get("requested_network_domains", []),
                    "dependencies": blueprint.get("requested_dependencies", []),
                    "reason": "Expected runtime permissions for this skill.",
                },
            }
        blueprint["permission_plan"] = self._sanitize_permission_plan(permission_source, fallback)
        return blueprint

    def _sanitize_permission_plan(self, value: object, fallback_plan: dict[str, object]) -> dict[str, object]:
        default_permissions = {
            "network": list(fallback_plan.get("requested_network_domains", []) or []),
            "filesystem_read": [],
            "filesystem_write": list(
                (fallback_plan.get("requested_permissions") or {}).get("filesystem_write", [])  # type: ignore[union-attr]
                if isinstance(fallback_plan.get("requested_permissions"), dict)
                else []
            ),
            "secrets": [],
            "shell": False,
        }
        if isinstance(fallback_plan.get("requested_permissions"), dict):
            default_permissions = {
                **default_permissions,
                **dict(fallback_plan["requested_permissions"]),  # type: ignore[index]
            }
        default_dependencies = list(fallback_plan.get("requested_dependencies", []) or [])
        default_network = list(fallback_plan.get("requested_network_domains", default_permissions.get("network", [])) or [])
        if not isinstance(value, dict):
            value = {}
        build_time = value.get("build_time") if isinstance(value.get("build_time"), dict) else {}
        runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
        permissions = runtime.get("permissions") if isinstance(runtime.get("permissions"), dict) else default_permissions
        sanitized_permissions = {
            "network": list(permissions.get("network", []) or []),
            "filesystem_read": list(permissions.get("filesystem_read", []) or []),
            "filesystem_write": list(permissions.get("filesystem_write", []) or []),
            "secrets": list(permissions.get("secrets", []) or []),
            "shell": bool(permissions.get("shell", False)),
        }
        network_domains = list(runtime.get("network_domains", sanitized_permissions["network"]) or [])
        dependencies = list(runtime.get("dependencies", default_dependencies) or [])
        return {
            "build_time": {
                "codex_generation": bool(build_time.get("codex_generation", True)),
                "internet_research": bool(build_time.get("internet_research", bool(network_domains or dependencies))),
                "dependencies": list(build_time.get("dependencies", dependencies) or []),
                "reason": str(build_time.get("reason") or "Codex needs to generate controlled skill files."),
            },
            "runtime": {
                "permissions": sanitized_permissions,
                "network_domains": network_domains or default_network,
                "dependencies": dependencies,
                "reason": str(runtime.get("reason") or "Expected runtime permissions for this skill."),
            },
        }

    def _sanitize_update_review(
        self,
        skill: Skill,
        suggestion: str,
        parsed: dict[str, object],
        fallback: dict[str, object],
    ) -> dict[str, object]:
        allowed = {
            "request_permission",
            "build_next_milestone",
            "run_tests",
            "repair_current_milestone",
            "ask_user_for_input",
            "finish_ready_for_review",
            "stop_failed",
            "stop_unsupported",
        }
        decision = parsed.get("decision")
        if decision not in allowed:
            return fallback
        summary = parsed.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = fallback["summary"]
        blueprint = self._sanitize_blueprint(parsed.get("blueprint"), fallback["blueprint"])  # type: ignore[arg-type]
        blueprint["skill_name"] = skill.name
        blueprint["skill_type"] = skill.skill_type
        blueprint["interface_type"] = skill.interface_type
        blueprint["suggestion"] = suggestion
        return {"decision": decision, "summary": summary, "blueprint": blueprint}

    def _sanitize_build_review(
        self,
        parsed: dict[str, object],
        fallback: dict[str, object],
    ) -> dict[str, object]:
        allowed = {"build_next_milestone", "ask_user_for_input", "stop_unsupported"}
        decision = parsed.get("decision")
        if decision not in allowed:
            decision = fallback["decision"]
        summary = parsed.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = fallback["summary"]
        reason = parsed.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = fallback["reason"]
        user_prompt = parsed.get("user_prompt")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            user_prompt = None
        optional_projects = parsed.get("optional_projects")
        if not isinstance(optional_projects, list):
            optional_projects = fallback["optional_projects"]
        optional_projects = [str(item) for item in optional_projects if str(item).strip()]
        return {
            "decision": decision,
            "summary": summary.strip(),
            "reason": reason.strip(),
            "user_prompt": user_prompt.strip() if user_prompt else None,
            "optional_projects": optional_projects,
        }

    def _product_manager_workspace(self) -> Path:
        workspace = self.project_root / "runtime" / "product_manager"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _fallback_build_blueprint(self, generation_request: SkillGenerationRequest) -> dict[str, object]:
        plan = generation_request.plan_json
        acceptance_criteria = [
            "manifest.json is valid",
            "required skill files exist",
            "automation or hybrid tests pass",
            "executable skills use JSON stdin/stdout",
        ]
        if plan.get("interface_type") == "tool":
            acceptance_criteria.append("tool_ui_schema is present so the Tools page can render a user-friendly UI")
        permission_plan = {
            "build_time": {
                "codex_generation": True,
                "internet_research": bool(plan.get("requested_network_domains") or plan.get("requested_dependencies")),
                "dependencies": plan.get("requested_dependencies", []),
                "reason": "Codex needs to generate proposed skill files.",
            },
            "runtime": {
                "permissions": plan.get(
                    "requested_permissions",
                    {"network": [], "filesystem_read": [], "filesystem_write": [], "secrets": [], "shell": False},
                ),
                "network_domains": plan.get("requested_network_domains", []),
                "dependencies": plan.get("requested_dependencies", []),
                "reason": "Runtime permissions expected by the proposed skill design.",
            },
        }
        return {
            "goal": plan.get("goal") or generation_request.user_message,
            "skill_name": plan.get("skill_name"),
            "skill_type": plan.get("skill_type"),
            "interface_type": plan.get("interface_type", "chat"),
            "expected_files": plan.get("files_to_generate", []),
            "expected_behavior": plan.get("expected_output", {}),
            "permission_plan": permission_plan,
            "milestones": [
                {
                    "name": "core_skill",
                    "summary": "Create the core proposed skill package.",
                    "acceptance_criteria": acceptance_criteria,
                }
            ],
        }

    def _fallback_repair_blueprint(self, skill: Skill, user_request: str | None) -> dict[str, object]:
        return {
            "goal": user_request or f"Repair {skill.name}.",
            "skill_name": skill.name,
            "skill_type": skill.skill_type,
            "interface_type": skill.interface_type,
            "expected_files": ["manifest.json", "README.md"],
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
            "skill_type": skill.skill_type,
            "interface_type": skill.interface_type,
            "suggestion": suggestion,
            "permission_plan": {
                "build_time": {
                    "codex_generation": True,
                    "internet_research": bool(requested_network_domains or requested_dependencies),
                    "dependencies": requested_dependencies,
                    "reason": "Codex needs to create a draft version for this update.",
                },
                "runtime": {
                    "permissions": {
                        "network": requested_network_domains,
                        "filesystem_read": [],
                        "filesystem_write": ["./cache"] if requested_network_domains else [],
                        "secrets": [],
                        "shell": False,
                    },
                    "network_domains": requested_network_domains,
                    "dependencies": requested_dependencies,
                    "reason": "Expected runtime permissions for the updated skill design.",
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

    def relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.project_root).as_posix()

    def _instruction(self, filename: str) -> str:
        path = self.instruction_dir / filename
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise CodexGenerationError(f"Missing agent instruction file: {filename}") from None
