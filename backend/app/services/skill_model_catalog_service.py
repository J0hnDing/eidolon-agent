from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.execution.skill_context import get_current_skill_id
from app.models import SkillModelCatalog
from app.schemas.skill_model import SkillModelRead
from app.services.codex_routing_service import CodexRoutingService


class SkillModelCatalogError(ValueError):
    pass


@dataclass
class SkillModelCatalogService:
    db: Session

    def read(self, skill_id: int) -> SkillModelRead:
        row = self.db.get(SkillModelCatalog, skill_id)
        return SkillModelRead(
            skill_id=skill_id,
            model=row.model if row is not None else None,
            reasoning_effort=row.reasoning_effort if row is not None else None,
        )

    def model_for_skill(self, skill_id: int | None) -> str | None:
        return self.settings_for_skill(skill_id)[0]

    def resolve_current_model(self, requested_model: str | None) -> str | None:
        return self.resolve_current_settings(requested_model, None)[0]

    def settings_for_skill(self, skill_id: int | None) -> tuple[str | None, str | None]:
        if skill_id is None:
            return None, None
        row = self.db.get(SkillModelCatalog, skill_id)
        return (
            row.model if row is not None else None,
            row.reasoning_effort if row is not None else None,
        )

    def resolve_current_settings(
        self,
        requested_model: str | None,
        requested_reasoning_effort: str | None,
    ) -> tuple[str | None, str | None]:
        model, reasoning_effort = self.settings_for_skill(get_current_skill_id())
        return model or requested_model, reasoning_effort or requested_reasoning_effort

    def update(
        self,
        skill_id: int,
        model: str | None,
        reasoning_effort: str | None = None,
    ) -> SkillModelRead:
        canonical_model, canonical_effort = self._canonical_selection(model, reasoning_effort)
        row = self.db.get(SkillModelCatalog, skill_id)
        if canonical_model is None:
            if row is not None:
                self.db.delete(row)
        elif row is None:
            self.db.add(
                SkillModelCatalog(
                    skill_id=skill_id,
                    model=canonical_model,
                    reasoning_effort=canonical_effort,
                )
            )
        else:
            row.model = canonical_model
            row.reasoning_effort = canonical_effort
        self.db.commit()
        return self.read(skill_id)

    def _canonical_selection(
        self,
        model: str | None,
        reasoning_effort: str | None,
    ) -> tuple[str | None, str | None]:
        if model is None and reasoning_effort is None:
            return None, None
        if model is None:
            raise SkillModelCatalogError("A reasoning effort requires a selected Codex model.")
        catalog = CodexRoutingService(self.db).read_model_catalog(refresh=True)
        if not catalog.get("available"):
            raise SkillModelCatalogError(
                "Codex model availability could not be validated before saving: "
                + str(catalog.get("error") or "model catalog unavailable")
            )
        for option in catalog.get("models", []):
            if not isinstance(option, dict):
                continue
            option_model = str(option.get("model") or "")
            option_id = str(option.get("id") or "")
            if model == option_model or model == option_id:
                if option_model:
                    supported_efforts = [
                        str(value)
                        for value in option.get("supported_reasoning_efforts", [])
                        if value
                    ]
                    if reasoning_effort is not None and reasoning_effort not in supported_efforts:
                        raise SkillModelCatalogError(
                            f"Codex model '{option_model}' does not support reasoning effort "
                            f"'{reasoning_effort}'. Supported values: "
                            f"{', '.join(supported_efforts) or 'none advertised'}."
                        )
                    return option_model, reasoning_effort
        raise SkillModelCatalogError(
            f"Codex model '{model}' is not available from the current CLI/account."
        )
