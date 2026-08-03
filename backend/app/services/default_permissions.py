import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_PERMISSION_POLICY_FILE = Path(__file__).resolve().parents[1] / "static" / "default_permissions.json"


def _validate_permission_template(value: object, path: str) -> None:
    if isinstance(value, dict):
        if not value:
            raise ValueError(f"Permission policy template {path!r} must not be empty")
        for key, nested_value in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"Permission policy template {path!r} has an invalid key")
            _validate_permission_template(nested_value, f"{path}.{key}")
        return
    if isinstance(value, list):
        if value:
            raise ValueError(f"Permission policy list template {path!r} must be empty")
        return
    if isinstance(value, bool):
        return
    raise ValueError(f"Permission policy template {path!r} has unsupported type {type(value).__name__}")


@lru_cache(maxsize=1)
def _load_default_permission_policy() -> dict[str, Any]:
    policy = json.loads(DEFAULT_PERMISSION_POLICY_FILE.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("Default permission policy must be a JSON object")
    required_sections = {
        "default_allowed": dict,
        "requires_approval": dict,
        "blocked": list,
        "web_app": dict,
    }
    for section, expected_type in required_sections.items():
        if not isinstance(policy.get(section), expected_type):
            raise ValueError(f"Default permission policy section {section!r} is missing or invalid")
    if not all(isinstance(item, str) and item.strip() for item in policy["blocked"]):
        raise ValueError("Default permission policy blocked entries must be non-empty strings")
    _validate_permission_template(policy["requires_approval"], "requires_approval")
    return policy


def default_permission_policy() -> dict[str, Any]:
    return deepcopy(_load_default_permission_policy())


def planning_permission_policy() -> dict[str, Any]:
    policy = default_permission_policy()
    return deepcopy(
        {
            "default_allowed": policy["default_allowed"],
            "requires_approval": policy["requires_approval"],
            "blocked": policy["blocked"],
        }
    )


def default_allowed_permissions() -> dict[str, Any]:
    return deepcopy(default_permission_policy()["default_allowed"])


def approval_required_permissions() -> dict[str, Any]:
    return deepcopy(default_permission_policy()["requires_approval"])


def blocked_permissions() -> list[str]:
    return list(default_permission_policy()["blocked"])


def agent_permission_bounds(permission_plan: dict[str, Any]) -> dict[str, Any]:
    build_time = permission_plan.get("build_time") if isinstance(permission_plan.get("build_time"), dict) else {}
    runtime = permission_plan.get("runtime") if isinstance(permission_plan.get("runtime"), dict) else {}
    return {
        "build_time": deepcopy(build_time),
        "runtime": deepcopy(runtime),
        "blocked": blocked_permissions(),
    }


def default_web_app_permissions() -> dict[str, list[str]]:
    policy = default_permission_policy()["web_app"]
    return {
        "supported": [str(item) for item in policy.get("supported", [])],
        "blocked": [str(item) for item in policy.get("blocked", [])],
    }


def default_build_time_dependencies() -> set[str]:
    build_time = default_allowed_permissions().get("build_time", {})
    dependencies = build_time.get("dependencies", []) if isinstance(build_time, dict) else []
    return {str(item).lower() for item in dependencies}


def effective_permission_plan(
    permission_plan: dict[str, Any],
) -> dict[str, Any]:
    runtime = permission_plan.get("runtime") if isinstance(permission_plan.get("runtime"), dict) else {}
    build_time = permission_plan.get("build_time") if isinstance(permission_plan.get("build_time"), dict) else {}
    defaults = default_allowed_permissions()
    default_build_time = defaults.get("build_time", {}) if isinstance(defaults.get("build_time"), dict) else {}
    default_runtime = defaults.get("runtime", {}) if isinstance(defaults.get("runtime"), dict) else {}

    build_dependencies = list(build_time.get("dependencies", []) or [])
    for dependency in default_build_time.get("dependencies", []) or []:
        if dependency not in build_dependencies:
            build_dependencies.append(dependency)

    filesystem_read = list(runtime.get("filesystem_read", []) or [])
    filesystem_write = list(runtime.get("filesystem_write", []) or [])
    codex = runtime.get("codex") if isinstance(runtime.get("codex"), dict) else {}
    effective_codex = {
        "call_response": bool(codex.get("call_response", default_runtime.get("codex", {}).get("call_response", True))),
        "internet_access": bool(codex.get("internet_access", False)),
    }
    for key, value in codex.items():
        if key not in effective_codex:
            effective_codex[str(key)] = bool(value)

    for path in default_runtime.get("filesystem_read", []) or []:
        if path not in filesystem_read:
            filesystem_read.append(path)
    for path in default_runtime.get("filesystem_write", []) or []:
        if path not in filesystem_write:
            filesystem_write.append(path)

    return {
        "build_time": {
            "internet_research": bool(build_time.get("internet_research", False)),
            "dependencies": build_dependencies,
            "project_read": list(default_build_time.get("project_read", []) or []),
        },
        "runtime": {
            "python_standard_library": bool(default_runtime.get("python_standard_library", True)),
            "network": list(runtime.get("network", []) or []),
            "filesystem_read": filesystem_read,
            "filesystem_write": filesystem_write,
            "secrets": list(runtime.get("secrets", []) or []),
            "shell": bool(runtime.get("shell", False)),
            "codex": effective_codex,
            "dependencies": list(runtime.get("dependencies", []) or []),
        },
    }
