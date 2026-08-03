import json
from pathlib import Path

from app.workflows.instructions import load_instruction

_PACKAGE_DIR = Path(__file__).resolve().parent


def build_prompt(
    blueprint: dict[str, object],
    permission_bounds: dict[str, object],
    output_dir: Path,
) -> str:
    instruction = load_instruction(_PACKAGE_DIR, "run.md")
    return f"""
{instruction}

Controlled skill folder:
{output_dir}

Blueprint:
{json.dumps(blueprint, indent=2)}

Permission bounds:
{json.dumps(permission_bounds, indent=2)}

Selected function context:
{json.dumps(blueprint.get("function_context", []), indent=2)}
""".strip()
