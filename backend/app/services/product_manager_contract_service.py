from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from app.models import Skill
from app.schemas.manifest import ManifestIntegrationResourceScope
from app.services.codex_output_schema import output_schema_for_action
from app.services.default_permissions import approval_required_permissions, default_build_time_dependencies


class ProductManagerContractError(ValueError):
    pass


class ProductManagerContractService:
    """Normalize untrusted ProductManager JSON into backend-owned contracts."""

    def sanitize_blueprint(self, value: object, fallback: dict[str, object]) -> dict[str, object]:
        if not isinstance(value, dict):
            value = {}
        allowed_fields = {
            "goal",
            "skill_name",
            "display_name",
            "runtime",
            "input_schema",
            "output_schema",
            "expected_behavior",
            "schedule",
            "acceptance_criteria",
            "milestones",
            "suggestion",
            "permission_plan",
            "functions",
            "integration_scopes",
        }
        blueprint = {key: fallback[key] for key in allowed_fields if key in fallback}
        blueprint.update({key: value[key] for key in allowed_fields if key in value})
        if not blueprint.get("goal"):
            blueprint["goal"] = fallback.get("goal")
        raw_skill_name = str(blueprint.get("skill_name") or fallback.get("skill_name") or "").strip()
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", raw_skill_name) is None:
            raw_skill_name = str(fallback.get("skill_name") or "generated_skill")
        blueprint["skill_name"] = raw_skill_name
        blueprint["display_name"] = str(
            blueprint.get("display_name")
            or fallback.get("display_name")
            or raw_skill_name.replace("_", " ").replace("-", " ").title()
        )[:256]
        runtime = blueprint.get("runtime") or fallback.get("runtime", "function")
        blueprint["runtime"] = runtime if runtime in {"function", "web_app", "service"} else "function"
        blueprint["input_schema"] = self._object_schema(
            blueprint.get("input_schema"),
            fallback.get("input_schema"),
        )
        blueprint["output_schema"] = self._object_schema(
            blueprint.get("output_schema"),
            fallback.get("output_schema"),
        )
        if blueprint["runtime"] in {"function", "service"} and (
            blueprint["input_schema"] is None or blueprint["output_schema"] is None
        ):
            blueprint["input_schema"] = {"type": "object", "additionalProperties": True}
            blueprint["output_schema"] = {"type": "object", "additionalProperties": True}
        if blueprint["runtime"] == "web_app":
            blueprint["input_schema"] = None
            blueprint["output_schema"] = None
        if not isinstance(blueprint.get("schedule"), dict):
            fallback_schedule = fallback.get("schedule")
            blueprint["schedule"] = fallback_schedule if isinstance(fallback_schedule, dict) else None
        if blueprint["runtime"] != "service":
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
        raw_functions = blueprint.get("functions")
        if not isinstance(raw_functions, list):
            raw_functions = fallback.get("functions", [])
        blueprint["functions"] = self._unique_strings(raw_functions)
        raw_scopes = blueprint.get("integration_scopes")
        if not isinstance(raw_scopes, dict):
            raw_scopes = fallback.get("integration_scopes", {})
        scopes: dict[str, object] = {}
        github_scope = raw_scopes.get("github") if isinstance(raw_scopes, dict) else None
        if isinstance(github_scope, dict):
            try:
                scopes["github"] = ManifestIntegrationResourceScope.model_validate(
                    github_scope
                ).model_dump(mode="json")
            except ValueError:
                pass
        if isinstance(raw_scopes, dict) and isinstance(raw_scopes.get("atlas"), dict):
            scopes["atlas"] = {}
        if isinstance(raw_scopes, dict) and isinstance(raw_scopes.get("notion"), dict):
            scopes["notion"] = {}
        blueprint["integration_scopes"] = scopes
        blueprint["permission_plan"] = self.sanitize_permission_plan(blueprint.get("permission_plan"), fallback)
        return blueprint

    @staticmethod
    def _unique_strings(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            normalized = str(item).strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _object_schema(value: object, fallback: object) -> dict[str, object] | None:
        candidate = value if isinstance(value, dict) else fallback
        if not isinstance(candidate, dict) or candidate.get("type") != "object":
            return None
        try:
            Draft202012Validator.check_schema(candidate)
        except SchemaError:
            return None
        return dict(candidate)

    def sanitize_permission_plan(self, value: object, fallback_plan: dict[str, object]) -> dict[str, object]:
        requested_permissions = fallback_plan.get("requested_permissions")
        requested_permissions = requested_permissions if isinstance(requested_permissions, dict) else {}
        requested_dependencies = list(fallback_plan.get("requested_dependencies", []) or [])
        requested_network = list(
            fallback_plan.get("requested_network_domains") or requested_permissions.get("network", []) or []
        )
        requested_codex = (
            requested_permissions.get("codex") if isinstance(requested_permissions.get("codex"), dict) else {}
        )
        fallback = {
            "build_time": {
                "internet_research": bool(requested_network or requested_dependencies),
                "dependencies": requested_dependencies,
            },
            "runtime": {
                "dependencies": requested_dependencies,
                "network": requested_network,
                "codex": {
                    "internet_access": bool(requested_codex.get("internet_access", requested_network)),
                },
            },
        }
        sanitized = self._sanitize_permission_template(value, approval_required_permissions(), fallback)
        sanitized["build_time"]["dependencies"] = [
            dependency
            for dependency in sanitized["build_time"]["dependencies"]
            if dependency.lower() not in default_build_time_dependencies()
        ]
        return sanitized

    def _sanitize_permission_template(self, value: object, template: object, fallback: object = None) -> Any:
        if isinstance(template, dict):
            source = value if isinstance(value, dict) else {}
            fallback_source = fallback if isinstance(fallback, dict) else {}
            return {
                key: self._sanitize_permission_template(
                    source.get(key, fallback_source.get(key)),
                    nested_template,
                    fallback_source.get(key),
                )
                for key, nested_template in template.items()
            }
        if isinstance(template, list):
            return self._unique_strings(value if value is not None else fallback)
        if isinstance(template, bool):
            return bool(fallback if value is None else value)
        raise ValueError(f"Unsupported permission policy template value: {type(template).__name__}")

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

    def sanitize_plan_build(
        self,
        parsed: dict[str, object],
        fallback_plan: dict[str, object],
    ) -> dict[str, object]:
        schema = output_schema_for_action("product_manager_plan_build")
        if schema is None:
            raise ProductManagerContractError("ProductManager planning schema is unavailable")
        errors = sorted(Draft202012Validator(schema).iter_errors(parsed), key=lambda error: list(error.path))
        if errors:
            detail = "; ".join(error.message for error in errors[:3])
            raise ProductManagerContractError(f"ProductManager returned an invalid planning response: {detail}")

        decision = str(parsed.get("decision") or "")
        user_prompt = parsed.get("user_prompt")
        build_workflow = parsed.get("build_workflow")
        blueprint = parsed.get("blueprint")
        permission_plan = parsed.get("permission_plan")

        if decision in {"ask_user_for_input", "stop_inplausible"}:
            if not isinstance(user_prompt, str) or not user_prompt.strip():
                raise ProductManagerContractError(
                    "ProductManager clarification or rejection must include a user-facing prompt"
                )
            if any(value is not None for value in (build_workflow, blueprint, permission_plan)):
                raise ProductManagerContractError(
                    "ProductManager clarification or rejection cannot include planning artifacts"
                )
            normalized_prompt = user_prompt.strip()
            if decision == "ask_user_for_input" and (
                not normalized_prompt.endswith("?") or normalized_prompt.count("?") != 1
            ):
                raise ProductManagerContractError(
                    "ProductManager clarification must contain exactly one question"
                )
            if decision == "stop_inplausible" and not re.search(
                r"\b(instead|alternative)\b",
                normalized_prompt,
                flags=re.IGNORECASE,
            ):
                raise ProductManagerContractError(
                    "ProductManager rejection must include a safe alternative"
                )
            return {
                "decision": decision,
                "user_prompt": normalized_prompt,
                "build_workflow": None,
                "blueprint": None,
                "permission_plan": None,
            }

        if decision != "proceed_to_approval":
            raise ProductManagerContractError("ProductManager returned an unknown planning decision")
        if user_prompt is not None:
            raise ProductManagerContractError("A completed ProductManager plan cannot include a user prompt")
        if build_workflow not in {"single_codex", "task_dag"}:
            raise ProductManagerContractError("A completed ProductManager plan requires a valid build workflow")
        if not isinstance(blueprint, dict) or not isinstance(permission_plan, dict):
            raise ProductManagerContractError("A completed ProductManager plan requires blueprint and permissions")

        runtime = blueprint.get("runtime")
        if runtime in {"function", "service"}:
            for field_name in ("input_schema", "output_schema"):
                callable_schema = blueprint.get(field_name)
                if not isinstance(callable_schema, dict) or callable_schema.get("type") != "object":
                    raise ProductManagerContractError(
                        f"A {runtime} blueprint requires a complete object-shaped {field_name}"
                    )
                try:
                    Draft202012Validator.check_schema(callable_schema)
                except SchemaError as exc:
                    raise ProductManagerContractError(
                        f"A {runtime} blueprint contains an invalid {field_name}"
                    ) from exc
            if runtime == "function" and blueprint.get("schedule") is not None:
                raise ProductManagerContractError("A function blueprint cannot include a schedule")
            if runtime == "service" and not isinstance(blueprint.get("schedule"), dict):
                raise ProductManagerContractError("A service blueprint requires a schedule")
        elif runtime == "web_app":
            if blueprint.get("input_schema") is not None or blueprint.get("output_schema") is not None:
                raise ProductManagerContractError(
                    "A web app blueprint must set input_schema and output_schema to null"
                )
            if blueprint.get("schedule") is not None:
                raise ProductManagerContractError("A web app blueprint cannot include a schedule")
        else:
            raise ProductManagerContractError("A completed ProductManager plan requires a valid runtime")

        sanitized_blueprint = self._sanitize_build_blueprint(blueprint)
        sanitized_permissions = self.sanitize_permission_plan(permission_plan, fallback_plan)
        return {
            "decision": decision,
            "user_prompt": None,
            "build_workflow": build_workflow,
            "blueprint": sanitized_blueprint,
            "permission_plan": sanitized_permissions,
        }

    def _sanitize_build_blueprint(self, blueprint: dict[str, object]) -> dict[str, object]:
        """Normalize the current build-only blueprint after strict schema validation."""
        return {
            "name": str(blueprint["name"]).strip(),
            "description": str(blueprint["description"]).strip(),
            "runtime": blueprint["runtime"],
            "input_schema": blueprint["input_schema"],
            "output_schema": blueprint["output_schema"],
            "expected_behavior": self._unique_strings(blueprint["expected_behavior"]),
            "functions": self._unique_strings(blueprint["functions"]),
            "schedule": blueprint["schedule"],
        }

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
            write_paths = self.skill_package_files(raw.get("write_paths"))
            criteria = raw.get("acceptance_criteria")
            sanitized_nodes.append(
                {
                    "id": node_id,
                    "task_prompt": str(raw.get("task_prompt") or "Build this task node."),
                    "depends_on": [str(item) for item in raw.get("depends_on", []) if str(item).strip()]
                    if isinstance(raw.get("depends_on"), list)
                    else [],
                    "difficulty": difficulty,
                    "requires_tests": bool(raw.get("requires_tests", False)),
                    "parallel_safe": bool(raw.get("parallel_safe", True)),
                    "write_paths": [path for path in write_paths if path != "manifest.json"],
                    "acceptance_criteria": [str(item) for item in criteria] if isinstance(criteria, list) else [],
                    "test_expectations": [str(item) for item in raw.get("test_expectations", [])]
                    if isinstance(raw.get("test_expectations"), list)
                    else [],
                    "function_ids": [
                        str(item) for item in raw.get("function_ids", []) if str(item).strip()
                    ]
                    if isinstance(raw.get("function_ids"), list)
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
