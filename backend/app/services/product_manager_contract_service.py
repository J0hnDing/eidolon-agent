from __future__ import annotations

from app.models import Skill
from app.services.default_permissions import default_build_time_dependencies


class ProductManagerContractService:
    """Normalize untrusted ProductManager JSON into backend-owned contracts."""

    def sanitize_blueprint(self, value: object, fallback: dict[str, object]) -> dict[str, object]:
        if not isinstance(value, dict):
            value = {}
        allowed_fields = {
            "goal",
            "skill_name",
            "skill_type",
            "interface_type",
            "expected_behavior",
            "schedule",
            "acceptance_criteria",
            "milestones",
            "suggestion",
            "permission_plan",
        }
        blueprint = {key: fallback[key] for key in allowed_fields if key in fallback}
        blueprint.update({key: value[key] for key in allowed_fields if key in value})
        for key in ("goal", "skill_name", "skill_type"):
            if not blueprint.get(key):
                blueprint[key] = fallback.get(key)
        blueprint["interface_type"] = blueprint.get("interface_type") or fallback.get("interface_type", "chat")
        if not isinstance(blueprint.get("schedule"), dict):
            fallback_schedule = fallback.get("schedule")
            blueprint["schedule"] = fallback_schedule if isinstance(fallback_schedule, dict) else None
        milestones = blueprint.get("milestones")
        if isinstance(milestones, list) and milestones:
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
        elif isinstance(fallback.get("milestones"), list) and fallback.get("milestones"):
            blueprint["milestones"] = fallback.get("milestones", [])
        else:
            blueprint.pop("milestones", None)
        criteria = blueprint.get("acceptance_criteria")
        if not isinstance(criteria, list):
            blueprint["acceptance_criteria"] = list(fallback.get("acceptance_criteria", []) or [])
        tool_criterion = "tool_ui_schema is present so the Tools page can render a user-friendly UI"
        if blueprint.get("interface_type") == "tool":
            criteria = blueprint.setdefault("acceptance_criteria", [])
            if isinstance(criteria, list) and tool_criterion not in criteria:
                criteria.append(tool_criterion)
        blueprint["permission_plan"] = self.sanitize_permission_plan(blueprint.get("permission_plan"), fallback)
        return blueprint

    def sanitize_permission_plan(self, value: object, fallback_plan: dict[str, object]) -> dict[str, object]:
        fallback_permissions = fallback_plan.get("requested_permissions")
        fallback_permissions = fallback_permissions if isinstance(fallback_permissions, dict) else {}
        default_permissions = {
            "network": list(fallback_plan.get("requested_network_domains", []) or []),
            "filesystem_read": [],
            "filesystem_write": [],
            "secrets": [],
            "shell": False,
        }
        if fallback_permissions:
            default_permissions = {**default_permissions, **dict(fallback_permissions)}
            for key in ("filesystem_read", "filesystem_write"):
                default_permissions[key] = [
                    path
                    for path in list(default_permissions.get(key, []) or [])
                    if str(path).replace("\\", "/").removeprefix("./").rstrip("/") != "cache"
                ]
        default_dependencies = list(fallback_plan.get("requested_dependencies", []) or [])
        if not isinstance(value, dict):
            value = {}
        build_time = value.get("build_time") if isinstance(value.get("build_time"), dict) else {}
        runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
        legacy_permissions = runtime.get("permissions") if isinstance(runtime.get("permissions"), dict) else None
        permissions = legacy_permissions if legacy_permissions is not None else {**default_permissions, **runtime}
        sanitized_permissions = {
            "network": list(permissions.get("network", []) or []),
            "filesystem_read": list(permissions.get("filesystem_read", []) or []),
            "filesystem_write": list(permissions.get("filesystem_write", []) or []),
            "secrets": list(permissions.get("secrets", []) or []),
            "shell": bool(permissions.get("shell", False)),
        }
        raw_codex = permissions.get("codex") if isinstance(permissions.get("codex"), dict) else {}
        sanitized_permissions["codex"] = {"internet_access": bool(raw_codex.get("internet_access", False))}
        network = sanitized_permissions["network"]
        dependencies = list(runtime.get("dependencies", default_dependencies) or [])
        build_time_dependencies = [
            dependency
            for dependency in list(build_time.get("dependencies", dependencies) or [])
            if str(dependency).lower() not in default_build_time_dependencies()
        ]
        return {
            "build_time": {
                "internet_research": bool(build_time.get("internet_research", bool(network or dependencies))),
                "dependencies": build_time_dependencies,
            },
            "runtime": {**sanitized_permissions, "dependencies": dependencies},
        }

    def sanitize_update_review(
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
            "repair_current_task",
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
        blueprint = self.sanitize_blueprint(parsed.get("blueprint"), fallback["blueprint"])  # type: ignore[arg-type]
        blueprint.update(
            {
                "skill_name": skill.name,
                "skill_type": skill.skill_type,
                "interface_type": skill.interface_type,
                "suggestion": suggestion,
            }
        )
        return {"decision": decision, "summary": summary, "blueprint": blueprint}

    @staticmethod
    def sanitize_build_review(parsed: dict[str, object], fallback: dict[str, object]) -> dict[str, object]:
        allowed = {"proceed_to_blueprint", "ask_user_for_input", "stop_inplausible"}
        decision = parsed.get("decision")
        if decision not in allowed:
            decision = fallback["decision"]
        user_prompt = parsed.get("user_prompt")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            user_prompt = None
        if decision != "proceed_to_blueprint" and user_prompt is None:
            user_prompt = (
                "Please clarify the requested skill."
                if decision == "ask_user_for_input"
                else "This request is not supported as an application skill."
            )
        return {"decision": decision, "user_prompt": user_prompt.strip() if user_prompt else None}

    def sanitize_task_dag(
        self,
        value: object,
        fallback: dict[str, object],
        blueprint: dict[str, object],
    ) -> dict[str, object]:
        del blueprint  # Reserved for contract rules that depend on the approved blueprint.
        if not isinstance(value, dict):
            value = fallback
        nodes = value.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            return fallback
        sanitized_nodes = []
        for raw in nodes:
            if not isinstance(raw, dict):
                continue
            node_id = str(raw.get("id") or "").strip()
            if not node_id:
                continue
            difficulty = str(raw.get("difficulty") or "medium")
            if difficulty not in {"easy", "medium", "hard"}:
                difficulty = "medium"
            expected_paths = self.skill_package_files(raw.get("expected_output_paths"))
            claims = self.skill_package_files(raw.get("file_write_claims")) or list(expected_paths)
            criteria = raw.get("acceptance_criteria")
            sanitized_nodes.append(
                {
                    "id": node_id,
                    "title": str(raw.get("title") or node_id.replace("_", " ").title()),
                    "summary": str(raw.get("summary") or "Build this task node."),
                    "depends_on": [str(item) for item in raw.get("depends_on", []) if str(item).strip()]
                    if isinstance(raw.get("depends_on"), list)
                    else [],
                    "difficulty": difficulty,
                    "requires_tests": bool(raw.get("requires_tests", False)),
                    "parallel_safe": bool(raw.get("parallel_safe", True)),
                    "expected_output_paths": expected_paths,
                    "file_write_claims": [path for path in claims if path != "manifest.json"],
                    "acceptance_criteria": [str(item) for item in criteria] if isinstance(criteria, list) else [],
                    "test_expectations": [str(item) for item in raw.get("test_expectations", [])]
                    if isinstance(raw.get("test_expectations"), list)
                    else [],
                    "interface_artifact_expectations": [
                        str(item) for item in raw.get("interface_artifact_expectations", [])
                    ]
                    if isinstance(raw.get("interface_artifact_expectations"), list)
                    else [],
                    "backend_api_ids": [
                        int(item) for item in raw.get("backend_api_ids", []) if str(item).strip().isdigit()
                    ]
                    if isinstance(raw.get("backend_api_ids"), list)
                    else [],
                }
            )
        return {"schema_version": 1, "nodes": sanitized_nodes} if sanitized_nodes else fallback

    @staticmethod
    def skill_package_files(value: object) -> list[str]:
        raw_files = value if isinstance(value, list) else []
        blocked_names = {
            "intent_prompt.json",
            "decision.json",
            "blueprint.json",
            "permissions.json",
            "task_dag.json",
        }
        files: list[str] = []
        for item in raw_files:
            path = str(item).replace("\\", "/").strip()
            if not path:
                continue
            if path in blocked_names or path.startswith("tasks/") or path.startswith("milestones/"):
                continue
            if path.startswith("tests/") or "/test_" in path or path.endswith("_test.py"):
                continue
            if path not in files:
                files.append(path)
        return files
