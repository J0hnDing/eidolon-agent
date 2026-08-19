from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

from app.services.default_permissions import approval_required_permissions


def _object_schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _described(schema: dict[str, object], description: str) -> dict[str, object]:
    return {**schema, "description": description}


_UNCONSTRAINED_OBJECT = {"type": "object"}


def _string_array_schema(*, min_items: int = 0, unique: bool = False) -> dict[str, object]:
    schema: dict[str, object] = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
    }
    if min_items:
        schema["minItems"] = min_items
    if unique:
        schema["uniqueItems"] = True
    return schema


def _schema_from_policy_template(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        properties = {str(key): _schema_from_policy_template(item) for key, item in value.items()}
        return _object_schema(properties, list(properties))
    if isinstance(value, list):
        return _string_array_schema()
    if isinstance(value, bool):
        return {"type": "boolean"}
    raise ValueError(f"Unsupported permission policy template value: {type(value).__name__}")


def permission_plan_output_schema() -> dict[str, object]:
    return _described(
        _schema_from_policy_template(approval_required_permissions()),
        "Build-time and expected runtime permissions that require user approval.",
    )


_CALLABLE_JSON_SCHEMA = {
    "anyOf": [
        _UNCONSTRAINED_OBJECT,
        {"type": "null"},
    ]
}

_MILESTONE_SCHEMA = _object_schema(
    {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "Stable short milestone name.",
        },
        "summary": {
            "type": "string",
            "minLength": 1,
            "description": "Work completed by this milestone.",
        },
        "acceptance_criteria": _described(
            _string_array_schema(min_items=1),
            "Observable conditions that complete this milestone.",
        ),
    },
    ["name", "summary", "acceptance_criteria"],
)

_SCHEDULE_SCHEMA = _described(
    {
        "anyOf": [
            {"type": "null"},
            _object_schema(
                {
                    "type": {"type": "string", "const": "daily"},
                    "time": {"type": "string", "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
                    "timezone": {"type": "string", "minLength": 1},
                    "input": _UNCONSTRAINED_OBJECT,
                },
                ["type", "time", "timezone", "input"],
            ),
            _object_schema(
                {
                    "type": {"type": "string", "const": "weekly"},
                    "day": {
                        "type": "string",
                        "enum": [
                            "monday",
                            "tuesday",
                            "wednesday",
                            "thursday",
                            "friday",
                            "saturday",
                            "sunday",
                        ],
                    },
                    "time": {"type": "string", "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
                    "timezone": {"type": "string", "minLength": 1},
                    "input": _UNCONSTRAINED_OBJECT,
                },
                ["type", "day", "time", "timezone", "input"],
            ),
            _object_schema(
                {
                    "type": {"type": "string", "const": "interval"},
                    "every": {"type": "integer", "minimum": 1},
                    "unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
                    "timezone": {"type": "string", "minLength": 1},
                    "input": _UNCONSTRAINED_OBJECT,
                },
                ["type", "every", "unit", "timezone", "input"],
            ),
        ]
    },
    "Recurring execution intent, or null when no schedule applies.",
)

_BUILD_BLUEPRINT_SCHEMA = _object_schema(
    {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z0-9_-]+$",
            "description": "Filesystem-safe skill identifier.",
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "description": "Concise description of the skill's purpose.",
        },
        "runtime": {
            "type": "string",
            "enum": ["function", "web_app"],
            "description": "Skill execution protocol.",
        },
        "input_schema": _described(
            _CALLABLE_JSON_SCHEMA,
            "Function input JSON Schema; null for a web app.",
        ),
        "output_schema": _described(
            _CALLABLE_JSON_SCHEMA,
            "Function output JSON Schema; null for a web app.",
        ),
        "expected_behavior": _described(
            _string_array_schema(min_items=1),
            "User-visible behavior the skill must provide.",
        ),
        "functions": _described(
            _string_array_schema(unique=True),
            "Exact selected function catalog identifiers.",
        ),
        "schedule": _SCHEDULE_SCHEMA,
    },
    [
        "name",
        "description",
        "runtime",
        "input_schema",
        "output_schema",
        "expected_behavior",
        "functions",
        "schedule",
    ],
)

_REPAIR_BLUEPRINT_SCHEMA = _object_schema(
    {
        "goal": {
            "type": "string",
            "minLength": 1,
            "description": "Concise repair objective.",
        },
        "skill_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z0-9_-]+$",
            "description": "Existing skill identifier.",
        },
        "runtime": {
            "type": "string",
            "enum": ["function", "web_app"],
            "description": "Existing skill execution protocol.",
        },
        "input_schema": _described(_CALLABLE_JSON_SCHEMA, "Current callable input contract."),
        "output_schema": _described(_CALLABLE_JSON_SCHEMA, "Current callable output contract."),
        "functions": _described(
            _string_array_schema(unique=True),
            "Exact function catalog identifiers used after repair.",
        ),
        "milestones": {
            "type": "array",
            "minItems": 1,
            "items": _MILESTONE_SCHEMA,
            "description": "Ordered repair milestones.",
        },
    },
    [
        "goal",
        "skill_name",
        "runtime",
        "input_schema",
        "output_schema",
        "functions",
        "milestones",
    ],
)

_UPDATE_BLUEPRINT_SCHEMA = _object_schema(
    {
        "goal": {
            "type": "string",
            "minLength": 1,
            "description": "Concise update objective.",
        },
        "skill_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z0-9_-]+$",
            "description": "Existing skill identifier.",
        },
        "runtime": {
            "type": "string",
            "enum": ["function", "web_app"],
            "description": "Existing skill execution protocol.",
        },
        "suggestion": {
            "type": "string",
            "minLength": 1,
            "description": "User-requested improvement.",
        },
        "input_schema": _described(_CALLABLE_JSON_SCHEMA, "Updated callable input contract."),
        "output_schema": _described(_CALLABLE_JSON_SCHEMA, "Updated callable output contract."),
        "functions": _described(
            _string_array_schema(unique=True),
            "Exact function catalog identifiers used after update.",
        ),
        "milestones": {
            "type": "array",
            "minItems": 1,
            "items": _MILESTONE_SCHEMA,
            "description": "Ordered update milestones.",
        },
        "permission_plan": {},
    },
    [
        "goal",
        "skill_name",
        "runtime",
        "suggestion",
        "input_schema",
        "output_schema",
        "functions",
        "milestones",
        "permission_plan",
    ],
)

_OUTPUT_SCHEMAS: dict[str, dict[str, object]] = {
    "atlas_knowledge_expand": _object_schema(
        {
            "terms": {
                "type": "array",
                "maxItems": 20,
                "items": _object_schema(
                    {
                        "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                        "label": {"type": "string", "minLength": 1, "maxLength": 80},
                        "definition": {"type": "string", "minLength": 1, "maxLength": 500},
                    },
                    ["id", "label", "definition"],
                ),
            },
            "children": {
                "type": "array",
                "maxItems": 30,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
        },
        ["terms", "children"],
    ),
    "atlas_knowledge_explain_expand": _object_schema(
        {
            "explanation": {"type": "string", "minLength": 1, "maxLength": 2000},
            "terms": {
                "type": "array",
                "maxItems": 20,
                "items": _object_schema(
                    {
                        "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                        "label": {"type": "string", "minLength": 1, "maxLength": 80},
                        "definition": {"type": "string", "minLength": 1, "maxLength": 500},
                    },
                    ["id", "label", "definition"],
                ),
            },
            "children": {
                "type": "array",
                "maxItems": 30,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
        },
        ["explanation", "terms", "children"],
    ),
    "product_manager_plan_build": _object_schema(
        {
            "decision": {
                "type": "string",
                "enum": ["ask_user_for_input", "stop_inplausible", "proceed_to_approval"],
                "description": "Clarify, reject, or return a complete build plan for approval.",
            },
            "user_prompt": {
                "type": ["string", "null"],
                "description": "One clarification question or rejection explanation; null when proceeding.",
            },
            "build_workflow": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "string",
                        "enum": ["single_codex", "task_dag"],
                    },
                ],
                "description": "Backend workflow used to build the skill; null unless proceeding.",
            },
            "blueprint": {
                "anyOf": [{"type": "null"}, _BUILD_BLUEPRINT_SCHEMA],
                "description": "Complete skill contract when proceeding; null otherwise.",
            },
            "permission_plan": {
                "anyOf": [{"type": "null"}, {}],
                "description": "Approval-required build/runtime permissions when proceeding; null otherwise.",
            },
        },
        ["decision", "user_prompt", "build_workflow", "blueprint", "permission_plan"],
    ),
    "product_manager_write_task_dag": _object_schema(
        {
            "task_dag": _described(
                _object_schema(
                    {
                        "schema_version": {
                            "type": "integer",
                            "const": 1,
                            "description": "Task DAG contract version.",
                        },
                        "nodes": {
                            "type": "array",
                            "minItems": 1,
                            "description": "Concrete build tasks in dependency order.",
                            "items": _object_schema(
                                {
                                    "id": {
                                        "type": "string",
                                        "pattern": "^[A-Za-z0-9_-]+$",
                                        "description": "Stable task identifier.",
                                    },
                                    "task_prompt": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": "Specific work assigned to BuilderAgent.",
                                    },
                                    "depends_on": {
                                        "type": "array",
                                        "uniqueItems": True,
                                        "items": {"type": "string"},
                                        "description": "Task identifiers that must complete first.",
                                    },
                                    "difficulty": {
                                        "type": "string",
                                        "enum": ["easy", "medium", "hard"],
                                        "description": "Estimated implementation difficulty.",
                                    },
                                    "requires_tests": {
                                        "type": "boolean",
                                        "description": "Whether TesterAgent must add tests for this task.",
                                    },
                                    "parallel_safe": {
                                        "type": "boolean",
                                        "description": "Whether the task can run beside independent tasks.",
                                    },
                                    "write_paths": {
                                        "type": "array",
                                        "minItems": 1,
                                        "uniqueItems": True,
                                        "items": {"type": "string"},
                                        "description": "Skill-package paths BuilderAgent must create or update.",
                                    },
                                    "acceptance_criteria": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {"type": "string"},
                                        "description": "Observable completion conditions for this task.",
                                    },
                                    "test_expectations": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Behavior TesterAgent should verify.",
                                    },
                                    "function_ids": {
                                        "type": "array",
                                        "uniqueItems": True,
                                        "items": {"type": "string"},
                                        "description": "Selected function identifiers used by this task.",
                                    },
                                },
                                [
                                    "id",
                                    "task_prompt",
                                    "depends_on",
                                    "difficulty",
                                    "requires_tests",
                                    "parallel_safe",
                                    "write_paths",
                                    "acceptance_criteria",
                                    "test_expectations",
                                    "function_ids",
                                ],
                            ),
                        },
                    },
                    ["schema_version", "nodes"],
                ),
                "Dependency graph for the approved skill build.",
            )
        },
        ["task_dag"],
    ),
    "product_manager_repair_blueprint": _object_schema(
        {
            "blueprint": _described(_REPAIR_BLUEPRINT_SCHEMA, "Bounded repair contract."),
            "decision": {
                "type": "string",
                "enum": ["repair_current_task", "ask_user_for_input", "stop_unsupported"],
                "description": "Next action for the repair workflow.",
            },
            "summary": {"type": "string", "description": "Short user-facing repair summary."},
        },
        ["blueprint", "decision", "summary"],
    ),
    "product_manager_update_review": _object_schema(
        {
            "decision": {
                "type": "string",
                "enum": [
                    "request_permission",
                    "build_next_milestone",
                    "ask_user_for_input",
                    "stop_unsupported",
                ],
                "description": "Next action for the update workflow.",
            },
            "summary": {"type": "string", "description": "Short user-facing update summary."},
            "blueprint": _described(_UPDATE_BLUEPRINT_SCHEMA, "Updated skill contract."),
        },
        ["decision", "summary", "blueprint"],
    ),
    "skill_runtime_codex": _object_schema(
        {
            "response": {"type": "string", "description": "Skill response to the caller."},
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional concise execution notes.",
            },
        },
        ["response", "notes"],
    ),
}


def output_schema_for_action(action: object) -> dict[str, object] | None:
    action_name = str(action or "")
    stored_schema = _OUTPUT_SCHEMAS.get(action_name)
    if stored_schema is None:
        return None
    schema = deepcopy(stored_schema)
    permission_schema = permission_plan_output_schema()
    if action_name == "product_manager_plan_build":
        schema["properties"]["permission_plan"]["anyOf"][1] = permission_schema  # type: ignore[index]
    elif action_name == "product_manager_update_review":
        schema["properties"]["blueprint"]["properties"]["permission_plan"] = permission_schema  # type: ignore[index]
    return schema


def write_temporary_output_schema(action: object, directory: Path) -> Path | None:
    schema = output_schema_for_action(action)
    if schema is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".codex-output-schema-",
        suffix=".json",
        dir=directory,
        text=True,
    )
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(schema, handle, separators=(",", ":"))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
