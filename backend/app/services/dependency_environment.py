import importlib.metadata
import json
import os
import sys
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

DEPENDENCY_STATE_FILE = ".personal-agent-dependencies.json"


class DependencyRequirementError(ValueError):
    pass


def normalize_requirements(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[str] = []
    for item in value:
        try:
            requirement = str(Requirement(str(item).strip()))
        except InvalidRequirement as exc:
            raise DependencyRequirementError(f"Invalid Python dependency requirement: {item!r}") from exc
        if requirement not in normalized:
            normalized.append(requirement)
    return normalized


def requirements_satisfied(
    requirements: list[str],
    *,
    paths: list[Path],
    include_global_environment: bool,
) -> bool:
    distributions = []
    if include_global_environment:
        distributions.extend(importlib.metadata.distributions())
    for path in paths:
        if path.is_dir():
            distributions.extend(importlib.metadata.distributions(path=[str(path)]))
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution.version
        for distribution in distributions
        if distribution.metadata.get("Name")
    }
    for raw_requirement in requirements:
        requirement = Requirement(raw_requirement)
        version = installed.get(canonicalize_name(requirement.name))
        if version is None or (requirement.specifier and not requirement.specifier.contains(version, prereleases=True)):
            return False
    return True


def read_dependency_state(path: Path) -> list[str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return [str(item) for item in value] if isinstance(value, list) else None


def build_dependency_environment(skill_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    dependency_paths = [skill_dir / ".deps", skill_dir / ".build-deps"]
    python_paths = [str(path) for path in dependency_paths if path.is_dir()]
    existing_python_path = env.get("PYTHONPATH")
    if existing_python_path:
        python_paths.append(existing_python_path)
    if python_paths:
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
    python_dir = str(Path(sys.executable).resolve().parent)
    existing_path = env.get("PATH")
    env["PATH"] = python_dir if not existing_path else f"{python_dir}{os.pathsep}{existing_path}"
    env["PERSONAL_AGENT_BUILD_PYTHON"] = str(Path(sys.executable).resolve())
    return env
