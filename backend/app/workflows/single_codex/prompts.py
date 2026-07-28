import json
from pathlib import Path

from app.services.integration_registry import operation_context
from app.workflows.instructions import load_instruction

_PACKAGE_DIR = Path(__file__).resolve().parent


def build_prompt(
    blueprint: dict[str, object],
    permission_plan: dict[str, object],
    output_dir: Path,
) -> str:
    instruction = load_instruction(_PACKAGE_DIR, "run.md")
    selected_operation_ids = [
        str(operation_id)
        for requirement in blueprint.get("integration_requirements", []) or []
        if isinstance(requirement, dict)
        for operation_id in requirement.get("operations", []) or []
    ]
    return f"""
{instruction}

Controlled skill folder:
{output_dir}

Blueprint:
{json.dumps(blueprint, indent=2)}

Effective permissions:
{json.dumps(permission_plan, indent=2)}

Selected integration operation context:
{json.dumps(operation_context(selected_operation_ids), indent=2)}
""".strip()
