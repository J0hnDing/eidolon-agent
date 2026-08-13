import json
from pathlib import Path

from app.workflows.base import ProjectBuildWorkflowError
from app.workflows.instructions import load_instruction

_PACKAGE_DIR = Path(__file__).resolve().parent
_INSTRUCTION_BY_TASK = {
    "refine_intent": "refine_intent.md",
    "plan_build": "plan_build.md",
}


def build_product_manager_prompt(task: str, payload: dict[str, object]) -> str:
    instruction_name = _INSTRUCTION_BY_TASK.get(task)
    if instruction_name is None:
        raise ProjectBuildWorkflowError(f"Unknown common project-build prompt task: {task}")
    instruction = load_instruction(_PACKAGE_DIR, instruction_name)
    return f"""
{instruction}

Task: {task}

Payload:
{json.dumps(payload, indent=2)}
""".strip()
