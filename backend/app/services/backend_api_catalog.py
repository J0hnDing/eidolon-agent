from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackendApiCatalogEntry:
    id: int
    title: str
    description: str
    context: dict[str, Any]

    def index_item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
        }


BACKEND_API_CATALOG: tuple[BackendApiCatalogEntry, ...] = (
    BackendApiCatalogEntry(
        id=1,
        title="Skill Codex Call API",
        description=(
            "Lets an installed skill ask the backend to call Codex for a bounded text response. "
            "Shell remains prohibited; Codex internet access is allowed only when the skill has runtime network access."
        ),
        context={
            "id": 1,
            "title": "Skill Codex Call API",
            "endpoint": "POST /skills/{skill_id}/codex",
            "runtime_environment": {
                "skill_id": "Read PERSONAL_AGENT_SKILL_ID.",
                "backend_url": "Read PERSONAL_AGENT_BACKEND_URL.",
            },
            "request_schema": {
                "prompt": "string",
                "context": "object with task-specific context for Codex",
                "model": "optional model name, for example gpt-5",
                "codex_permissions": {
                    "call_response": True,
                    "internet_access": "optional bool; true requires approved runtime network access",
                },
            },
            "response_schema": {
                "response": "string",
                "model": "string or null",
                "internet_access": "bool",
            },
            "permission_rules": [
                "manifest.permissions.codex.call_response is granted by default and must remain true to call this API",
                "manifest.permissions.network and runtime approval are required before requesting codex_permissions.internet_access=true",
                "shell access and filesystem access through Codex are not available through this API",
            ],
        },
    ),
    BackendApiCatalogEntry(
        id=2,
        title="Tool UI Schema API",
        description=(
            "Declarative manifest fields for rendering an installed tool in the Tools UI. "
            "This is metadata, not app frontend code."
        ),
        context={
            "id": 2,
            "title": "Tool UI Schema API",
            "where": "manifest.json",
            "fields": ["interface_type", "input_schema", "output_schema", "tool_ui_schema"],
            "supported_field_types": ["text", "number", "textarea", "checkbox", "select"],
            "rules": [
                "Set interface_type to tool for installed enabled automation skills that should appear in Tools.",
                "Define tool_ui_schema with labels, field names, supported field types, options/defaults where needed, and result rendering hints.",
                "Do not generate React, HTML, JavaScript, or application frontend files for tool UI work.",
            ],
        },
    ),
)


def backend_api_index() -> list[dict[str, Any]]:
    return [entry.index_item() for entry in BACKEND_API_CATALOG]


def backend_api_context(api_ids: object) -> list[dict[str, Any]]:
    if not isinstance(api_ids, list):
        return []
    requested: list[int] = []
    for raw_id in api_ids:
        try:
            api_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if api_id not in requested:
            requested.append(api_id)
    by_id = {entry.id: entry for entry in BACKEND_API_CATALOG}
    return [by_id[api_id].context for api_id in requested if api_id in by_id]


def valid_backend_api_ids() -> set[int]:
    return {entry.id for entry in BACKEND_API_CATALOG}
