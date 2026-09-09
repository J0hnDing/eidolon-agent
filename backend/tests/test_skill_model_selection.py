import asyncio
import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.execution.skill_context import current_skill_id, skill_execution_context
from app.models import Skill, SkillModelCatalog
from app.schemas.skill_codex import SkillCodexRequest
from app.services.codex_routing_service import CodexRoutingService
from app.services.codex_service import CodexService, RealCodexAdapter
from app.services.skill_model_catalog_service import SkillModelCatalogService


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def make_skill(db: Session, name: str) -> Skill:
    skill = Skill(
        name=name,
        description=name,
        status="installed",
        manifest_path=f"skills/installed/{name}/manifest.json",
        enabled=True,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def test_concurrent_skill_contexts_are_isolated() -> None:
    async def observe(skill_id: int) -> tuple[int | None, int | None]:
        with skill_execution_context(skill_id):
            before = current_skill_id.get()
            await asyncio.sleep(0)
            after = current_skill_id.get()
            return before, after

    async def run() -> list[tuple[int | None, int | None]]:
        return await asyncio.gather(observe(11), observe(22))

    assert asyncio.run(run()) == [(11, 11), (22, 22)]
    assert current_skill_id.get() is None


def test_nested_skill_context_restores_parent() -> None:
    assert current_skill_id.get() is None
    with skill_execution_context(101):
        assert current_skill_id.get() == 101
        with skill_execution_context(202):
            assert current_skill_id.get() == 202
        assert current_skill_id.get() == 101
    assert current_skill_id.get() is None


def test_skill_model_catalog_persists_and_clears_override(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = make_skill(db_session, "model_catalog_skill")
    monkeypatch.setattr(
        CodexRoutingService,
        "read_model_catalog",
        lambda _self, refresh=False: {
            "available": True,
            "models": [
                {
                    "id": "model-id",
                    "model": "gpt-skill",
                    "supported_reasoning_efforts": ["low", "high"],
                }
            ],
        },
    )

    service = SkillModelCatalogService(db_session)
    saved = service.update(skill.id, "model-id", "high")
    assert saved.model == "gpt-skill"
    assert saved.reasoning_effort == "high"
    row = db_session.get(SkillModelCatalog, skill.id)
    assert row.model == "gpt-skill"
    assert row.reasoning_effort == "high"
    assert service.update(skill.id, None).model is None
    assert db_session.get(SkillModelCatalog, skill.id) is None


class RecordingCodexAdapter:
    def __init__(self) -> None:
        self.plans: list[dict] = []

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        del prompt, output_dir
        self.plans.append(plan)
        return subprocess.CompletedProcess(
            args=["recording-codex"],
            returncode=0,
            stdout=json.dumps({"response": "ok"}),
            stderr="",
        )


def test_codex_model_override_resolution_and_fallback(
    db_session: Session,
    tmp_path: Path,
) -> None:
    skill = make_skill(db_session, "model_resolution_skill")
    db_session.add(SkillModelCatalog(skill_id=skill.id, model="gpt-skill", reasoning_effort="high"))
    db_session.commit()
    adapter = RecordingCodexAdapter()
    service = CodexService(db_session, adapter=adapter, project_root=tmp_path)
    payload = SkillCodexRequest(
        prompt="Use the configured model.",
        model="gpt-request",
        reasoning_effort="low",
    )

    with skill_execution_context(skill.id):
        overridden = service.skill_runtime_codex_call(skill, payload, internet_access=False)
    assert adapter.plans[-1]["model"] == "gpt-skill"
    assert adapter.plans[-1]["reasoning_effort"] == "high"
    assert overridden["model"] == "gpt-skill"

    fallback = service.skill_runtime_codex_call(skill, payload, internet_access=False)
    assert adapter.plans[-1]["model"] == "gpt-request"
    assert adapter.plans[-1]["reasoning_effort"] == "low"
    assert fallback["model"] == "gpt-request"
    assert current_skill_id.get() is None


def test_real_codex_adapter_receives_skill_model_and_effort_override(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = make_skill(db_session, "real_adapter_resolution_skill")
    db_session.add(SkillModelCatalog(skill_id=skill.id, model="gpt-skill", reasoning_effort="high"))
    db_session.commit()
    observed: dict[str, str | None] = {}

    def fake_generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        del prompt, output_dir, plan
        observed["model"] = self.model
        observed["reasoning_effort"] = self.reasoning_effort
        return subprocess.CompletedProcess(
            args=["recording-codex"],
            returncode=0,
            stdout=json.dumps({"response": "ok"}),
            stderr="",
        )

    monkeypatch.setattr(RealCodexAdapter, "generate", fake_generate)
    adapter = RealCodexAdapter(command="codex", model="gpt-global", reasoning_effort="low")
    service = CodexService(db_session, adapter=adapter, project_root=tmp_path)

    with skill_execution_context(skill.id):
        service.skill_runtime_codex_call(
            skill,
            SkillCodexRequest(prompt="Use the configured effort."),
            internet_access=False,
        )

    assert observed == {"model": "gpt-skill", "reasoning_effort": "high"}
