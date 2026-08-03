from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import CodexRoutingSettings
from app.schemas.codex_routing import (
    CodexRoutingSettingsPayload,
    CodexRoutingSettingsRead,
    InvocationChoice,
    ResolvedInvocationSettings,
)
from app.services.codex_usage_service import CodexUsageService, codex_usage_service


class CodexRoutingError(ValueError):
    pass


_PM_ACTIONS = {
    "product_manager_refine_intent": "refine_intent",
    "product_manager_build_review": "plausibility_review",
    "product_manager_write_blueprint_and_permissions": "blueprint_and_permissions",
    "product_manager_write_task_dag": "task_dag",
    "product_manager_repair_blueprint": "repair",
    "product_manager_update_review": "update",
}


@dataclass
class CodexRoutingService:
    db: Session
    catalog_service: CodexUsageService = codex_usage_service

    def read_settings(self) -> CodexRoutingSettingsRead:
        row = self.db.get(CodexRoutingSettings, 1)
        payload = self._payload(row.settings_json if row is not None else {})
        return CodexRoutingSettingsRead(**payload.model_dump(mode="json"), updated_at=row.updated_at if row else None)

    def update_settings(self, payload: CodexRoutingSettingsPayload) -> CodexRoutingSettingsRead:
        catalog = self.catalog_service.read_model_catalog(refresh=True)
        self._validate_all(payload, catalog)
        row = self.db.get(CodexRoutingSettings, 1)
        if row is None:
            row = CodexRoutingSettings(id=1, settings_json=payload.model_dump(mode="json"))
            self.db.add(row)
        else:
            row.settings_json = payload.model_dump(mode="json")
        self.db.commit()
        self.db.refresh(row)
        return CodexRoutingSettingsRead(**payload.model_dump(mode="json"), updated_at=row.updated_at)

    def read_model_catalog(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.catalog_service.read_model_catalog(refresh=refresh)

    def project_build_workflow_override(self) -> str | None:
        row = self.db.get(CodexRoutingSettings, 1)
        return self._payload(row.settings_json if row is not None else {}).project_build_workflow_override

    def resolve(
        self,
        *,
        role: str,
        action: str,
        difficulty: str | None = None,
        model_override: str | None = None,
        reasoning_effort_override: str | None = None,
    ) -> ResolvedInvocationSettings:
        payload = self._payload((self.db.get(CodexRoutingSettings, 1) or CodexRoutingSettings()).settings_json)
        catalog = self.catalog_service.read_model_catalog()
        return self._resolve_from_payload(
            payload,
            catalog,
            role=role,
            action=action,
            difficulty=difficulty,
            model_override=model_override,
            reasoning_effort_override=reasoning_effort_override,
        )

    def _resolve_from_payload(
        self,
        payload: CodexRoutingSettingsPayload,
        catalog: dict[str, Any],
        *,
        role: str,
        action: str,
        difficulty: str | None,
        model_override: str | None = None,
        reasoning_effort_override: str | None = None,
    ) -> ResolvedInvocationSettings:
        default_choice, specific_choice, route_source = self._route_choices(
            payload, role=role, action=action, difficulty=difficulty
        )
        requested_model = model_override or specific_choice.model or default_choice.model
        requested_effort = reasoning_effort_override or specific_choice.reasoning_effort or default_choice.reasoning_effort
        if model_override or reasoning_effort_override:
            route_source = "invocation_override"
        configured_model = os.getenv("PERSONAL_AGENT_CODEX_MODEL") or None
        configured_effort = os.getenv("PERSONAL_AGENT_CODEX_REASONING_EFFORT") or None
        selected_model = requested_model or configured_model
        selected_effort = requested_effort or configured_effort

        models = [item for item in catalog.get("models", []) if isinstance(item, dict)]
        if not catalog.get("available"):
            if selected_model or selected_effort:
                raise CodexRoutingError(
                    "Codex model availability could not be validated before the invocation: "
                    + str(catalog.get("error") or "model catalog unavailable")
                )
            return ResolvedInvocationSettings(
                role=role,
                action=action,
                difficulty=difficulty,
                route_source=route_source,
            )

        model_by_name = {
            str(key): item
            for item in models
            for key in (item.get("model"), item.get("id"))
            if key
        }
        if selected_model is None:
            default_model = next((item for item in models if item.get("is_default")), models[0] if models else None)
            selected_model = str(default_model.get("model")) if default_model else None
        model_info = model_by_name.get(selected_model or "")
        if selected_model and model_info is None:
            raise CodexRoutingError(f"Codex model '{selected_model}' is not available from the current CLI/account.")
        if selected_effort is None and model_info is not None:
            selected_effort = str(model_info.get("default_reasoning_effort") or "") or None
        supported_efforts = list(model_info.get("supported_reasoning_efforts") or []) if model_info else []
        if selected_effort and selected_effort not in supported_efforts:
            raise CodexRoutingError(
                f"Codex model '{selected_model}' does not support reasoning effort '{selected_effort}'. "
                f"Supported values: {', '.join(supported_efforts) or 'none advertised'}."
            )
        return ResolvedInvocationSettings(
            role=role,
            action=action,
            difficulty=difficulty,
            route_source=route_source,
            requested_model=requested_model,
            effective_model=selected_model,
            requested_reasoning_effort=requested_effort,
            effective_reasoning_effort=selected_effort,
        )

    def _validate_all(self, payload: CodexRoutingSettingsPayload, catalog: dict[str, Any]) -> None:
        routes = [
            ("chat", "chat", None),
            *[("product_manager", action, None) for action in _PM_ACTIONS],
            ("builder", "single_codex_build", None),
            *[("builder", "skill_build_task", difficulty) for difficulty in ("easy", "medium", "hard")],
            ("builder", "skill_repair", None),
            ("builder", "skill_update", None),
            ("tester", "tester_write_tests", None),
            ("tester", "tester_final_e2e", None),
            ("tester", "tester_update", None),
        ]
        for role, action, difficulty in routes:
            self._resolve_from_payload(payload, catalog, role=role, action=action, difficulty=difficulty)

    @staticmethod
    def _payload(value: dict[str, Any]) -> CodexRoutingSettingsPayload:
        return CodexRoutingSettingsPayload.model_validate(value or {})

    @staticmethod
    def _route_choices(
        payload: CodexRoutingSettingsPayload,
        *,
        role: str,
        action: str,
        difficulty: str | None,
    ) -> tuple[InvocationChoice, InvocationChoice, str]:
        empty = InvocationChoice()
        if role == "chat":
            return empty, payload.chat, "chat"
        if role == "product_manager":
            route = _PM_ACTIONS.get(action)
            specific = getattr(payload.product_manager, route) if route else empty
            source = f"product_manager.{route}" if route and (specific.model or specific.reasoning_effort) else "product_manager.default"
            return payload.product_manager.default, specific, source
        if role == "builder":
            route = difficulty if action in {"skill_build_task", "skill_generation"} and difficulty else None
            if action == "single_codex_build":
                route = "single_codex"
            elif action in {"skill_repair", "skill_update_repair"}:
                route = "repair"
            elif action == "skill_update":
                route = "update"
            specific = (
                getattr(payload.builder, route)
                if route in {"single_codex", "easy", "medium", "hard", "repair", "update"}
                else empty
            )
            source = f"builder.{route}" if route and (specific.model or specific.reasoning_effort) else "builder.default"
            return payload.builder.default, specific, source
        if role == "tester":
            route = "update" if action == "tester_update" else "final_e2e" if action == "tester_final_e2e" else "task"
            specific = getattr(payload.tester, route)
            source = f"tester.{route}" if specific.model or specific.reasoning_effort else "tester.default"
            return payload.tester.default, specific, source
        raise CodexRoutingError(f"Unknown Codex routing role: {role}")
