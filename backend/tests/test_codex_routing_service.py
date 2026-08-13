import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.schemas.codex_routing import CodexRoutingSettingsPayload, ResolvedInvocationSettings
from app.services.codex_routing_service import CodexRoutingError, CodexRoutingService
from app.services.codex_service import RealCodexAdapter

CATALOG = {
    "available": True,
    "fetched_at": "2026-07-12T00:00:00+00:00",
    "error": None,
    "models": [
        {
            "id": "gpt-fast",
            "model": "gpt-fast",
            "display_name": "GPT Fast",
            "description": "Fast model",
            "is_default": True,
            "default_reasoning_effort": "medium",
            "supported_reasoning_efforts": ["low", "medium"],
        },
        {
            "id": "gpt-smart",
            "model": "gpt-smart",
            "display_name": "GPT Smart",
            "description": "Smart model",
            "is_default": False,
            "default_reasoning_effort": "high",
            "supported_reasoning_efforts": ["medium", "high", "xhigh"],
        },
    ],
}


class FakeCatalogService:
    def read_model_catalog(self, *, refresh: bool = False) -> dict:
        return CATALOG


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def test_builder_routes_by_task_difficulty_and_inherits_role_defaults(db_session: Session) -> None:
    payload = CodexRoutingSettingsPayload.model_validate(
        {
            "builder": {
                "default": {"model": "gpt-fast", "reasoning_effort": "low"},
                "hard": {"model": "gpt-smart", "reasoning_effort": "high"},
            }
        }
    )
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())
    service.update_settings(payload)

    easy = service.resolve(role="builder", action="skill_build_task", difficulty="easy")
    hard = service.resolve(role="builder", action="skill_build_task", difficulty="hard")

    assert easy.effective_model == "gpt-fast"
    assert easy.effective_reasoning_effort == "low"
    assert easy.route_source == "builder.default"
    assert hard.effective_model == "gpt-smart"
    assert hard.effective_reasoning_effort == "high"
    assert hard.route_source == "builder.hard"


def test_single_codex_builder_has_independent_route(db_session: Session) -> None:
    payload = CodexRoutingSettingsPayload.model_validate(
        {
            "builder": {
                "default": {"model": "gpt-fast", "reasoning_effort": "low"},
                "single_codex": {"model": "gpt-smart", "reasoning_effort": "xhigh"},
            }
        }
    )
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())
    service.update_settings(payload)

    resolved = service.resolve(role="builder", action="single_codex_build")

    assert resolved.effective_model == "gpt-smart"
    assert resolved.effective_reasoning_effort == "xhigh"
    assert resolved.route_source == "builder.single_codex"


def test_settings_validate_single_codex_builder_choice(db_session: Session) -> None:
    payload = CodexRoutingSettingsPayload.model_validate(
        {
            "builder": {
                "single_codex": {"model": "gpt-fast", "reasoning_effort": "xhigh"},
            }
        }
    )
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())

    with pytest.raises(CodexRoutingError, match="does not support reasoning effort 'xhigh'"):
        service.update_settings(payload)


def test_project_build_workflow_override_round_trips_and_defaults_to_automatic(db_session: Session) -> None:
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())

    assert service.read_settings().project_build_workflow_override is None
    assert service.project_build_workflow_override() is None

    payload = CodexRoutingSettingsPayload(project_build_workflow_override="single_codex")
    saved = service.update_settings(payload)

    assert saved.project_build_workflow_override == "single_codex"
    assert service.project_build_workflow_override() == "single_codex"


def test_product_manager_action_override_is_independent(db_session: Session) -> None:
    payload = CodexRoutingSettingsPayload.model_validate(
        {
            "product_manager": {
                "default": {"model": "gpt-fast", "reasoning_effort": "low"},
                "task_dag": {"model": "gpt-smart", "reasoning_effort": "xhigh"},
            }
        }
    )
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())
    service.update_settings(payload)

    resolved = service.resolve(role="product_manager", action="product_manager_write_task_dag")

    assert resolved.effective_model == "gpt-smart"
    assert resolved.effective_reasoning_effort == "xhigh"
    assert resolved.route_source == "product_manager.task_dag"


@pytest.mark.parametrize("legacy_action", ["skill_plan", "project_plausibility"])
def test_removed_product_manager_actions_use_only_the_default_route(
    db_session: Session,
    legacy_action: str,
) -> None:
    payload = CodexRoutingSettingsPayload.model_validate(
        {
            "product_manager": {
                "default": {"model": "gpt-fast", "reasoning_effort": "low"},
                "blueprint_and_permissions": {
                    "model": "gpt-smart",
                    "reasoning_effort": "high",
                },
            }
        }
    )
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())
    service.update_settings(payload)

    resolved = service.resolve(role="product_manager", action=legacy_action)

    assert resolved.effective_model == "gpt-fast"
    assert resolved.effective_reasoning_effort == "low"
    assert resolved.route_source == "product_manager.default"


def test_product_manager_plan_build_uses_blueprint_and_permissions_route(db_session: Session) -> None:
    payload = CodexRoutingSettingsPayload.model_validate(
        {
            "product_manager": {
                "default": {"model": "gpt-fast", "reasoning_effort": "low"},
                "blueprint_and_permissions": {
                    "model": "gpt-smart",
                    "reasoning_effort": "high",
                },
            }
        }
    )
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())
    service.update_settings(payload)

    resolved = service.resolve(role="product_manager", action="product_manager_plan_build")

    assert resolved.effective_model == "gpt-smart"
    assert resolved.effective_reasoning_effort == "high"
    assert resolved.route_source == "product_manager.blueprint_and_permissions"


def test_settings_reject_effort_not_advertised_by_selected_model(db_session: Session) -> None:
    payload = CodexRoutingSettingsPayload.model_validate(
        {"chat": {"model": "gpt-fast", "reasoning_effort": "xhigh"}}
    )
    service = CodexRoutingService(db_session, catalog_service=FakeCatalogService())

    with pytest.raises(CodexRoutingError, match="does not support reasoning effort 'xhigh'"):
        service.update_settings(payload)


def test_real_adapter_applies_effective_model_and_effort_and_records_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        stdout = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("app.services.codex_service.subprocess.run", fake_run)
    settings = ResolvedInvocationSettings(
        role="builder",
        action="skill_build_task",
        difficulty="hard",
        route_source="builder.hard",
        requested_model="gpt-smart",
        effective_model="gpt-smart",
        requested_reasoning_effort="high",
        effective_reasoning_effort="high",
    )
    adapter = RealCodexAdapter(command="codex", enable_search="false").with_invocation_settings(
        settings, sandbox_mode="workspace-write"
    )

    result = adapter.generate("Build it", tmp_path, {})

    assert captured[captured.index("--model") + 1] == "gpt-smart"
    assert 'model_reasoning_effort="high"' in captured
    assert result.codex_requested_model == "gpt-smart"
    assert result.codex_model == "gpt-smart"
    assert result.codex_reasoning_effort == "high"
    assert result.codex_route_source == "builder.hard"
