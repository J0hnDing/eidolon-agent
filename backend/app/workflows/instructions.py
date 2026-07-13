from pathlib import Path

from app.workflows.base import ProjectBuildWorkflowError


def load_instruction(package_dir: Path, relative_path: str) -> str:
    instruction_root = (package_dir / "instructions").resolve()
    requested = Path(relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        raise ProjectBuildWorkflowError(f"Invalid workflow instruction path: {relative_path}")
    path = (instruction_root / requested).resolve()
    if not path.is_relative_to(instruction_root):
        raise ProjectBuildWorkflowError(f"Invalid workflow instruction path: {relative_path}")
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ProjectBuildWorkflowError(f"Missing workflow instruction file: {relative_path}") from None
