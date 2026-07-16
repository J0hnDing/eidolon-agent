import json
from pathlib import Path
from typing import Any

from app.workflows.instructions import load_instruction

_PACKAGE_DIR = Path(__file__).resolve().parent


def build_product_manager_prompt(payload: dict[str, object]) -> str:
    instruction = load_instruction(_PACKAGE_DIR, "product_manager.md")
    return f"""
{instruction}

Task: write_task_dag

Payload:
{json.dumps(payload, indent=2)}
""".strip()


def build_builder_prompt(plan: dict, output_dir: Path, *, builder_writes_tests: bool = False) -> str:
    instruction = load_instruction(_PACKAGE_DIR, "builder.md")
    builder_context = plan.get("task_context")
    if not isinstance(builder_context, dict):
        builder_context = plan
    test_requirement = (
        "- tests must not require installing packages"
        if builder_writes_tests
        else "- do not create, modify, or delete tests. TesterAgent will inspect the implementation and write tests separately."
    )
    return f"""
{instruction}

Write files only inside this exact folder:
{output_dir}

Builder context:
{json.dumps(builder_context, indent=2)}

Product structure:
- Follow only the permission bounds, current task node, direct-parent interface artifacts, selected backend API context, and workspace paths in Builder context.
- Build only the current task node and respect its file_write_claims.
- Treat the folder above as the only writable workspace. Do not write by absolute path or traverse outside it.
- Write interface_artifact.json at the controlled skill-folder root. Do not write directly under runtime/agent_runs; the backend validates and moves the sidecar there.
- Do not write blueprint.json, permissions.json, task_dag.json, or task JSON files; those are backend-owned workflow artifacts.
- Read existing files directly from the listed workspace paths; their contents are intentionally not duplicated in the prompt.
- The backend seeds manifest.json from approved product and permission artifacts. Preserve its security fields and complete only fields owned by this task.
- TesterAgent owns test files in this workflow unless builder_writes_tests is explicitly true.

Skill runtime requirements:
- read JSON from stdin
- write JSON object to stdout
- handle errors by returning JSON where possible
- no side effects on import
- use a main guard
{test_requirement}
- include SKILL.md only when reusable instructions or operating guidance are useful
""".strip()


def build_repair_prompt(skill: Any, output_dir: Path, failure_context: dict) -> str:
    instruction = load_instruction(_PACKAGE_DIR, "repair.md")
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


def build_tester_prompt(output_dir: Path, tester_context: dict) -> str:
    instruction = load_instruction(_PACKAGE_DIR, "tester.md")
    return f"""
{instruction}

Controlled skill folder:
{output_dir}

Tester context:
{json.dumps(tester_context, indent=2)}
""".strip()
