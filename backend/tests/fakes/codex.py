import json
import subprocess
from pathlib import Path

from app.services.codex_service import (
    CodexGenerationError,
    _fallback_runtime,
    _fallback_schedule,
    _fallback_skill_identity,
    _web_app_smoke_test_source,
)
from app.services.default_permissions import default_build_time_dependencies
from app.workflows.base import DEFAULT_BUILD_WORKFLOW


class DeterministicCodexStub:
    """Small test-only Codex substitute for deterministic service tests."""

    uses_codex_account_quota = False

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        output_dir.mkdir(parents=True, exist_ok=True)
        task = plan.get("codex_task")
        if task == "product_manager_plan_build":
            blueprint = self._blueprint(plan, str(plan.get("user_message", "")))
            return self._json_result(
                {
                    "decision": "proceed_to_approval",
                    "user_prompt": None,
                    "build_workflow": DEFAULT_BUILD_WORKFLOW,
                    "blueprint": blueprint,
                    "permission_plan": self._permission_plan(plan.get("generation_plan", {})),
                },
                "product-manager-plan-build",
            )
        if task == "product_manager_write_task_dag":
            return self._json_result(
                {"task_dag": self._task_dag(plan.get("blueprint_json", {}), plan.get("generation_plan", {}))},
                "product-manager-task-dag",
            )
        if task == "product_manager_repair_blueprint":
            return self._json_result(
                {
                    "blueprint": {
                        "goal": plan.get("user_request") or f"Repair {plan['skill_name']}.",
                        "skill_name": plan["skill_name"],
                        "runtime": plan.get("runtime", "function"),
                        "milestones": [
                            {
                                "name": "repair_skill",
                                "summary": "Repair the proposed skill package and confirm tests pass.",
                                "acceptance_criteria": ["manifest.json is valid", "tests pass"],
                            }
                        ],
                    }
                },
                "product-manager-repair",
            )
        if task == "product_manager_update_review":
            return self._json_result(self._update_decision(plan), "product-manager-update")
        if task == "product_manager_summary":
            return self._json_result(
                {"summary": plan.get("fallback_summary", "ProductManager completed the review.")},
                "product-manager-summary",
            )
        if task == "tester_write_tests":
            self._write_tests(output_dir, plan)
            return self._result("tester")
        if task == "skill_update":
            readme_path = output_dir / "README.md"
            existing = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
            readme_path.write_text(
                existing.rstrip()
                + "\n\n## Proposed Update\n\n"
                + str(plan.get("suggestion", "Improve this skill.")).strip()
                + "\n",
                encoding="utf-8",
            )
            return self._result("skill-update")
        if task == "skill_update_repair":
            return self._result("skill-update-repair")
        if task == "skill_runtime_codex":
            return self._json_result({"response": "Deterministic Codex response.", "notes": []}, "skill-runtime")
        if task in {"atlas_knowledge_expand", "atlas_knowledge_explain_expand"}:
            payload = {
                "terms": [{"id": "core-concept", "label": "Core concept", "definition": "Test definition."}],
                "children": ["Immediate subtopic"],
            }
            if task == "atlas_knowledge_explain_expand":
                payload["explanation"] = "Test explanation."
            return self._json_result(payload, "atlas-knowledge")
        self._write_skill(output_dir, plan)
        return self._result("builder")

    @staticmethod
    def _result(label: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[f"test-{label}"], returncode=0, stdout="ok", stderr="")

    @staticmethod
    def _json_result(payload: dict, label: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[f"test-{label}"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    def _write_skill(self, output_dir: Path, plan: dict) -> None:
        runtime = str(plan.get("runtime") or "function")
        permissions = plan.get("requested_permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        manifest = {
            "manifest_version": 1,
            "name": plan["skill_name"],
            "description": plan.get("description") or plan.get("goal") or plan["skill_name"],
            "runtime": runtime,
            "entrypoint": "app:app" if runtime == "web_app" else "skill.py",
            "instructions_path": self._instructions_path(plan),
            "input_schema": plan.get("input_schema"),
            "output_schema": plan.get("output_schema"),
            "function_requirements": list(plan.get("function_requirements", []) or []),
            "integration_requirements": list(plan.get("integration_requirements", []) or []),
            "dependencies": list(plan.get("requested_dependencies", []) or []),
            "permissions": permissions,
            "schedule": plan.get("schedule"),
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (output_dir / "README.md").write_text("# Generated Skill\n", encoding="utf-8")
        if manifest["instructions_path"]:
            (output_dir / "SKILL.md").write_text("# Instructions\n", encoding="utf-8")
        if runtime == "web_app":
            (output_dir / "app.py").write_text(
                "from fastapi import FastAPI\nfrom fastapi.responses import HTMLResponse\n\n"
                "app = FastAPI()\n\n@app.get('/', response_class=HTMLResponse)\n"
                "def index():\n    return '<html><body>Generated Web Application</body></html>'\n",
                encoding="utf-8",
            )
        else:
            (output_dir / "skill.py").write_text(
                "import json\nimport sys\n\n"
                "payload = json.loads(sys.stdin.read() or '{}')\n"
                "print(json.dumps({'title': 'Generated Proposed Skill', 'items': [], "
                "'input': payload, 'warnings': []}))\n",
                encoding="utf-8",
            )
        if plan.get("builder_writes_tests", True):
            self._write_tests(output_dir, plan)
        self._write_interface_artifact(output_dir, plan)

    def _write_tests(self, output_dir: Path, plan: dict) -> None:
        tests_dir = output_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        requested = str(plan.get("test_file") or "").strip()
        task_id = str(plan.get("task_id") or (plan.get("task_node") or {}).get("id") or "").strip()
        relative = requested or (f"tests/test_{task_id}.py" if task_id else "tests/test_skill.py")
        test_path = output_dir / relative
        if test_path.parent != tests_dir:
            raise CodexGenerationError("Tester requires a direct tests/ file")
        if plan.get("runtime") == "web_app":
            test_path.write_text(_web_app_smoke_test_source(), encoding="utf-8")
            return
        test_path.write_text(
            "import json\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n"
            "ROOT = Path(__file__).resolve().parents[1]\n\n"
            "def test_manifest_matches_blueprint_and_safe_contract():\n"
            "    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))\n"
            "    assert 'shell' not in manifest['permissions']\n"
            "    assert 'secrets' not in manifest['permissions']\n\n"
            "def test_skill_accepts_representative_input_and_outputs_json_object():\n"
            "    result = subprocess.run([sys.executable, str(ROOT / 'skill.py')], input='{}', "
            "capture_output=True, text=True, timeout=5, shell=False)\n"
            "    assert result.returncode == 0, result.stderr\n"
            "    assert isinstance(json.loads(result.stdout), dict)\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_interface_artifact(output_dir: Path, plan: dict) -> None:
        context = plan.get("task_context")
        task_node = context.get("task_node") if isinstance(context, dict) else None
        if not isinstance(task_node, dict):
            return
        paths = [
            str(path).replace("\\", "/").removeprefix("./")
            for path in task_node.get("write_paths", []) or []
            if (output_dir / str(path)).is_file()
        ]
        artifact = {
            "created_paths": [path for path in paths if path != "manifest.json"],
            "updated_paths": [path for path in paths if path == "manifest.json"],
            "interfaces": {
                "entrypoint": "app:app" if (output_dir / "app.py").is_file() else "skill.py",
                "input_schema": plan.get("input_schema") or {},
                "output_schema": plan.get("output_schema") or {},
            },
            "contracts_for_children": ["Children may use the declared interfaces."],
            "known_limitations": [],
        }
        (output_dir / "interface_artifact.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    @staticmethod
    def _permission_plan(plan: object) -> dict:
        source = plan if isinstance(plan, dict) else {}
        dependencies = list(source.get("requested_dependencies", []) or [])
        build_dependencies = [
            item for item in dependencies if str(item).lower() not in default_build_time_dependencies()
        ]
        requested = source.get("requested_permissions")
        requested = requested if isinstance(requested, dict) else {}
        codex = requested.get("codex") if isinstance(requested.get("codex"), dict) else {}
        return {
            "build_time": {
                "internet_research": bool(source.get("requested_network_domains") or dependencies),
                "dependencies": build_dependencies,
            },
            "runtime": {
                "dependencies": dependencies,
                "network": list(source.get("requested_network_domains", []) or []),
                "codex": {
                    "call_response": bool(codex.get("call_response", False)),
                    "internet_access": bool(codex.get("internet_access", False)),
                },
            },
        }

    @staticmethod
    def _blueprint(plan: dict, user_message: str) -> dict:
        identity = _fallback_skill_identity(user_message)
        runtime = _fallback_runtime(user_message)
        available_ids = {
            str(entry.get("id"))
            for entry in plan.get("function_catalog_index", []) or []
            if isinstance(entry, dict)
        }
        functions = []
        if "github" in user_message.lower() and "trending" in user_message.lower():
            if "github.repository.trending.list" in available_ids:
                functions.append("github.repository.trending.list")
        return {
            "name": identity["skill_name"],
            "description": user_message,
            "runtime": runtime,
            "input_schema": {"type": "object", "additionalProperties": True}
            if runtime in {"function", "service"}
            else None,
            "output_schema": {"type": "object", "additionalProperties": True}
            if runtime in {"function", "service"}
            else None,
            "expected_behavior": ["Implement the requested reusable capability."],
            "functions": functions,
            "schedule": _fallback_schedule(user_message) if runtime == "service" else None,
        }

    @classmethod
    def _task_dag(cls, blueprint: object, plan: object) -> dict:
        blueprint = blueprint if isinstance(blueprint, dict) else {}
        plan = plan if isinstance(plan, dict) else {}
        files = cls._skill_package_files(plan.get("files_to_generate") or ["manifest.json"])
        return {
            "schema_version": 1,
            "nodes": [
                {
                    "id": "core_skill",
                    "task_prompt": "Create the core proposed skill package.",
                    "depends_on": [],
                    "difficulty": "easy",
                    "requires_tests": True,
                    "parallel_safe": True,
                    "write_paths": [path for path in files if path != "manifest.json"],
                    "acceptance_criteria": list(
                        blueprint.get("expected_behavior") or ["manifest.json is valid", "tests pass"]
                    ),
                    "test_expectations": ["validate manifest and generated behavior"],
                    "function_ids": list(blueprint.get("functions", []) or []),
                }
            ],
        }

    @staticmethod
    def _skill_package_files(value: object) -> list[str]:
        files: list[str] = []
        for item in value if isinstance(value, list) else []:
            path = str(item).replace("\\", "/").strip()
            if not path or path.startswith("tests/") or path in {
                "intent_prompt.json",
                "decision.json",
                "blueprint.json",
                "permissions.json",
                "task_dag.json",
            }:
                continue
            if path not in files:
                files.append(path)
        return files

    @staticmethod
    def _instructions_path(plan: dict) -> str | None:
        files = plan.get("files_to_generate")
        return "SKILL.md" if isinstance(files, list) and "SKILL.md" in files else None

    @staticmethod
    def _update_decision(plan: dict) -> dict:
        suggestion = str(plan.get("suggestion", "")).strip()
        summary = f"Update {plan['skill_name']} with this improvement: {suggestion}"
        return {
            "decision": "build_next_milestone",
            "summary": summary,
            "blueprint": {
                "goal": summary,
                "skill_name": plan["skill_name"],
                "runtime": plan.get("runtime", "function"),
                "suggestion": suggestion,
                "permission_plan": {
                    "build_time": {"internet_research": False, "dependencies": []},
                    "runtime": {
                        "network": [],
                        "filesystem_read": [],
                        "filesystem_write": [],
                        "secrets": [],
                        "shell": False,
                        "codex": {"call_response": False, "internet_access": False},
                        "dependencies": [],
                    },
                },
                "requested_network_domains": [],
                "requested_dependencies": [],
                "milestones": [
                    {
                        "name": "update_version",
                        "summary": "Create and validate the draft update.",
                        "acceptance_criteria": ["draft version tests pass"],
                    }
                ],
            },
        }
