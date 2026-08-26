from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Skill
from app.services.atlas_knowledge_service import codex_available
from app.services.integration_registry import OPERATIONS
from app.services.integration_service import build_default_integration_service

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
FUNCTION_CATALOG_SEED_PATH = STATIC_DIR / "function_catalog_seed.json"


class FunctionCatalogError(ValueError):
    pass


@dataclass
class FunctionCatalogService:
    """Own the unified PM, Builder, runtime-discovery, and UI function catalog."""

    db: Session
    project_root: Path | None = None

    def __post_init__(self) -> None:
        if self.project_root is None:
            self.project_root = Path(__file__).resolve().parents[3]
        self.project_root = self.project_root.resolve()
        self.catalog_path = self.project_root / "runtime" / "function_catalog.json"

    def list_entries(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            self.refresh()
        payload = self._read_catalog()
        entries = payload.get("functions", [])
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    def available_index(self) -> list[dict[str, str]]:
        return [
            {
                "id": str(entry["id"]),
                "category": str(entry["category"]),
                "title": str(entry["title"]),
                "description": str(entry["description"]),
                "risk_level": str(entry["risk_level"]),
            }
            for entry in self.list_entries()
            if entry.get("availability") == "available"
        ]

    def context(self, function_ids: object, *, available_only: bool = True) -> list[dict[str, Any]]:
        selected_ids = self.normalize_ids(function_ids)
        by_id = {str(entry["id"]): entry for entry in self.list_entries()}
        contexts: list[dict[str, Any]] = []
        for function_id in selected_ids:
            entry = by_id.get(function_id)
            if entry is None:
                continue
            if available_only and entry.get("availability") != "available":
                continue
            contexts.append(dict(entry))
        return contexts

    def validate_available_ids(self, function_ids: object) -> list[str]:
        selected_ids = self.normalize_ids(function_ids)
        by_id = {str(entry["id"]): entry for entry in self.list_entries()}
        for function_id in selected_ids:
            entry = by_id.get(function_id)
            if entry is None:
                raise FunctionCatalogError(f"Unknown function id: {function_id}")
            if entry.get("availability") != "available":
                reasons = entry.get("availability_reasons") or ["Function is unavailable"]
                raise FunctionCatalogError(f"Function {function_id} is unavailable: {'; '.join(reasons)}")
        return selected_ids

    def user_function_names(self, function_ids: object) -> list[str]:
        return [
            str(entry["call_name"])
            for entry in self.context(function_ids)
            if entry.get("category") == "user"
        ]

    def integration_operation_ids(self, function_ids: object) -> list[str]:
        return [
            str(entry["id"])
            for entry in self.context(function_ids)
            if entry.get("category") == "integration"
        ]

    def backend_core_ids(self, function_ids: object) -> list[str]:
        return [
            str(entry["id"])
            for entry in self.context(function_ids)
            if entry.get("category") == "backend_core"
        ]

    def refresh(self) -> None:
        fixed_entries = self._fixed_entries()
        user_entries = self._user_entries()
        payload = {
            "schema_version": 1,
            "functions": sorted(
                [*fixed_entries, *user_entries],
                key=lambda entry: (str(entry["category"]), str(entry["title"]).casefold(), str(entry["id"])),
            ),
        }
        self._write_catalog(payload)

    def register_user_function(self, skill: Skill) -> None:
        if skill.status == "installed" and skill.runtime == "function":
            self.refresh()

    def remove_user_function(self, _skill_name: str) -> None:
        self.refresh()

    @staticmethod
    def normalize_ids(function_ids: object) -> list[str]:
        if not isinstance(function_ids, list):
            return []
        normalized: list[str] = []
        for raw_function_id in function_ids:
            function_id = str(raw_function_id).strip()
            if function_id and function_id not in normalized:
                normalized.append(function_id)
        return normalized

    def _fixed_entries(self) -> list[dict[str, Any]]:
        seed = self._read_json(FUNCTION_CATALOG_SEED_PATH)
        entries = []
        for raw_entry in seed.get("functions", []):
            if not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            if entry.get("category") == "backend_core":
                entry.setdefault("mcp_exposed", False)
            scheduler_only = entry.pop("scheduler_only", False) is True
            entries.append(
                self._with_availability(
                    entry,
                    not scheduler_only,
                    ["Scheduler-only backend function"] if scheduler_only else [],
                )
            )
        integrations = build_default_integration_service(self.db)
        provider_state = {
            provider: integrations.provider_connected(provider)
            for provider in {operation.provider for operation in OPERATIONS.values()}
        }
        atlas_codex_available = codex_available()
        for operation in OPERATIONS.values():
            connected = provider_state[operation.provider]
            unavailable_reason = {
                "github": "GitHub connection is not configured",
                "atlas": "Atlas is not running and unlocked",
                "notion": "Notion connection is not configured",
            }[operation.provider]
            reasons = [] if connected else [unavailable_reason]
            if operation.operation_id == "atlas.knowledge.node.know" and not atlas_codex_available:
                reasons.append("A compatible Codex CLI is unavailable")
            available = connected and not reasons
            entries.append(
                self._with_availability(
                    {
                        "id": operation.operation_id,
                        "category": "integration",
                        "title": operation.title,
                        "description": operation.description,
                        "risk_level": operation.risk,
                        "input_schema": operation.input_schema,
                        "output_schema": operation.output_schema,
                        "provider": operation.provider,
                        "invocation": operation.agent_context(),
                        "mcp_exposed": True,
                        "mcp_read_only": operation.read_only,
                        "mcp_destructive": operation.operation_id == "notion.todo.delete",
                        "mcp_open_world": operation.provider in {"github", "notion"},
                        "mcp_contract_fingerprint": (
                            f"{operation.operation_id}:v{operation.contract_version}"
                        ),
                    },
                    available,
                    reasons,
                )
            )
        return entries

    def _user_entries(self) -> list[dict[str, Any]]:
        from app.services.function_registry_service import FunctionRegistryService
        from app.services.proposed_skill_service import ProposedSkillService

        ProposedSkillService(self.db, project_root=self.project_root).sync_installed_from_filesystem()
        registry = FunctionRegistryService(self.db, project_root=self.project_root)
        skills = self.db.scalars(
            select(Skill)
            .where(Skill.status == "installed")
            .where(Skill.runtime == "function")
            .order_by(Skill.name)
        ).all()
        entries: list[dict[str, Any]] = []
        for skill in skills:
            contract = registry.contract_for_skill(skill)
            permissions = contract.permissions
            integration_providers = {
                str(requirement.get("provider"))
                for requirement in (skill.integration_requirements_json or [])
                if isinstance(requirement, dict)
            }
            entries.append(
                {
                    "id": skill.name,
                    "category": "user",
                    "title": skill.name,
                    "description": contract.description,
                    "risk_level": contract.risk_level,
                    "input_schema": contract.input_schema,
                    "output_schema": contract.output_schema,
                    "call_name": skill.name,
                    "skill_id": skill.id,
                    "active_version": contract.active_version,
                    "availability": contract.availability,
                    "availability_reasons": contract.availability_reasons,
                    "invocation": {
                        "function_helper": (
                            f'function_runtime_capabilities.call_function("{skill.name}", input_json)'
                        ),
                        "web_app_helper": (
                            f'web_runtime_capabilities.call_function("{skill.name}", input_json)'
                        ),
                        "test_guidance": "Mock the runtime helper and assert input/output JSON contract handling.",
                    },
                    "mcp_exposed": True,
                    "mcp_read_only": False,
                    "mcp_destructive": False,
                    "mcp_open_world": bool(permissions.get("network"))
                    or bool(integration_providers.intersection({"github", "notion"})),
                    "mcp_contract_fingerprint": (
                        registry.target_contract_fingerprint(skill)
                        if contract.availability == "available"
                        else None
                    ),
                }
            )
        return entries

    @staticmethod
    def _with_availability(
        entry: dict[str, Any],
        available: bool,
        reasons: list[str],
    ) -> dict[str, Any]:
        entry["availability"] = "available" if available else "unavailable"
        entry["availability_reasons"] = reasons
        return entry

    def _read_catalog(self) -> dict[str, Any]:
        if not self.catalog_path.is_file():
            self.refresh()
        return self._read_json(self.catalog_path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise FunctionCatalogError(f"Function catalog must contain a JSON object: {path}")
        return payload

    def _write_catalog(self, payload: dict[str, Any]) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.catalog_path.with_name(
            f".{self.catalog_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.catalog_path)
