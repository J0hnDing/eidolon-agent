from __future__ import annotations

from app.models import Skill
from app.schemas.manifest import ManifestIntegrationRequirement
from app.services.default_permissions import default_build_time_dependencies


class ProductManagerContractService:
    """Normalize untrusted ProductManager JSON into backend-owned contracts."""

    def sanitize_blueprint(self, value: object, fallback: dict[str, object]) -> dict[str, object]:
        if not isinstance(value, dict):
            value = {}
        allowed_fields = {
            "goal",
            "skill_name",
            "runtime",
            "expected_behavior",
            "schedule",
            "acceptance_criteria",
            "milestones",
            "suggestion",
            "permission_plan",
            "function_requirements",
            "integration_requirements",
        }
        blueprint = {key: fallback[key] for key in allowed_fields if key in fallback}
        blueprint.update({key: value[key] for key in allowed_fields if key in value})
        if not blueprint.get("goal"):
            blueprint["goal"] = fallback.get("goal")
        # The backend-selected package identity is stable across every PM action.
        # ProductManager may improve the display/goal wording but cannot rename
        # the controlled folder or database record mid-workflow.
        blueprint["skill_name"] = fallback.get("skill_name")
        runtime = blueprint.get("runtime") or fallback.get("runtime", "function")
        blueprint["runtime"] = runtime if runtime in {"function", "web_app"} else "function"
        if not isinstance(blueprint.get("schedule"), dict):
            fallback_schedule = fallback.get("schedule")
            blueprint["schedule"] = fallback_schedule if isinstance(fallback_schedule, dict) else None
        if blueprint["runtime"] == "web_app":
            blueprint["schedule"] = None
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
        raw_requirements = blueprint.get("function_requirements")
        if not isinstance(raw_requirements, list):
            raw_requirements = fallback.get("function_requirements", [])
        requirements: list[dict[str, str]] = []
        seen_names: set[str] = set()
        for item in raw_requirements if isinstance(raw_requirements, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not name or not reason or name in seen_names:
                continue
            requirements.append({"name": name, "reason": reason})
            seen_names.add(name)
        blueprint["function_requirements"] = requirements
        raw_integrations = blueprint.get("integration_requirements")
        if not isinstance(raw_integrations, list):
            raw_integrations = fallback.get("integration_requirements", [])
        integrations: list[dict[str, object]] = []
        seen_providers: set[str] = set()
        for item in raw_integrations if isinstance(raw_integrations, list) else []:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip()
            operations = item.get("operations")
            scope = item.get("resource_scope")
            reason = str(item.get("reason") or "").strip()
            if (
                provider != "github"
                or provider in seen_providers
                or not isinstance(operations, list)
                or not isinstance(scope, dict)
                or not reason
            ):
                continue
            try:
                normalized = ManifestIntegrationRequirement.model_validate(
                    {
                        "provider": provider,
                        "operations": [str(operation) for operation in operations],
                        "resource_scope": {
                            "repositories": [str(repository) for repository in scope.get("repositories", [])]
                            if isinstance(scope.get("repositories"), list)
                            else []
                        },
                        "reason": reason,
                    }
                )
            except ValueError:
                continue
            integrations.append(normalized.model_dump(mode="json"))
            seen_providers.add(provider)
        blueprint["integration_requirements"] = integrations
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
                "runtime": skill.runtime,
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
                    "integration_operation_ids": [
                        str(item) for item in raw.get("integration_operation_ids", []) if str(item).strip()
                    ]
                    if isinstance(raw.get("integration_operation_ids"), list)
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
