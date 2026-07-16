import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_PERMISSION_POLICY_FILE = Path(__file__).resolve().parents[1] / "static" / "default_permissions.json"


@lru_cache(maxsize=1)
def default_permission_policy() -> dict[str, Any]:
    return json.loads(DEFAULT_PERMISSION_POLICY_FILE.read_text(encoding="utf-8"))


def default_allowed_permissions() -> dict[str, Any]:
    return dict(default_permission_policy().get("default_allowed", {}))


def default_banned_permissions() -> list[str]:
    return [str(item) for item in default_permission_policy().get("banned_permissions", [])]


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
