import json
import subprocess
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill, SkillRun
from app.routers import skills as skills_router
from app.routers.skills import call_codex_for_skill
from app.schemas.skill_codex import SkillCodexRequest
from app.services.codex_service import CodexGenerationError, CodexService
from app.services.permission_service import PermissionService


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def write_installed_skill(project_root: Path, *, network: list[str] | None = None) -> Path:
    skill_dir = project_root / "skills" / "installed" / "codex_skill"
    (skill_dir / "tests").mkdir(parents=True)
    manifest = {
        "name": "codex_skill",
        "description": "Calls Codex through the backend.",
        "interface_type": "chat",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "risk_level": "medium" if network else "low",
        "permissions": {
            "network": network or [],
            "filesystem_read": [],
            "filesystem_write": [],
            "secrets": [],
            "shell": False,
            "codex": {"call_response": True, "internet_access": bool(network)},
        },
        "schedule": None,
        "created_by": "test",
        "enabled": False,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{}')\n", encoding="utf-8")
    (skill_dir / "tests" / "test_skill.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return skill_dir


def create_skill(db: Session) -> Skill:
    skill = Skill(
        name="codex_skill",
        description="Calls Codex through the backend.",
        interface_type="chat",
        status="installed",
        risk_level="low",
        manifest_path="skills/installed/codex_skill/manifest.json",
        installed_path="skills/installed/codex_skill",
        enabled=True,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def approve_runtime(db: Session, skill: Skill, project_root: Path) -> None:
    permission_service = PermissionService(db, project_root=project_root)
    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)


def test_skill_codex_call_uses_backend_and_requires_runtime_approval(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_installed_skill(tmp_path)
    monkeypatch.setattr(skills_router, "PROJECT_ROOT", tmp_path)
    skill = create_skill(db_session)
    approve_runtime(db_session, skill, tmp_path)

    response = call_codex_for_skill(
        skill.id,
        SkillCodexRequest(prompt="Summarize this.", context={"item": "demo"}, model="gpt-5"),
        db_session,
    )

    assert response.response == "Fake Codex response."
    assert response.model == "gpt-5"
    assert response.internet_access is False


def test_skill_codex_internet_requires_runtime_network(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_installed_skill(tmp_path)
    monkeypatch.setattr(skills_router, "PROJECT_ROOT", tmp_path)
    skill = create_skill(db_session)
    approve_runtime(db_session, skill, tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        call_codex_for_skill(
            skill.id,
            SkillCodexRequest(
                prompt="Research this.",
                codex_permissions={"call_response": True, "internet_access": True},
            ),
            db_session,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Codex internet access requires approved runtime network permission"


class UsageCodexAdapter:
    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        result = subprocess.CompletedProcess(
            args=["usage-codex"],
            returncode=0,
            stdout='{"response": "Tracked response."}',
            stderr="",
        )
        result.codex_usage = {
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "output_tokens": 30,
            "reasoning_output_tokens": 10,
            "total_tokens": 150,
        }
        result.codex_adapter = "test_runtime_adapter"
        result.codex_model = "gpt-test"
        result.codex_requested_model = "gpt-test"
        return result


class FailingCodexAdapter:
    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["failing-codex"],
            returncode=1,
            stdout="",
            stderr="request context omitted\nERROR: account quota is exhausted\n",
        )


def test_skill_runtime_codex_tokens_are_added_to_the_active_skill_run(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session)
    run = SkillRun(
        skill_id=skill.id,
        status="running",
        input_json={},
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.commit()

    service = CodexService(db_session, adapter=UsageCodexAdapter(), project_root=tmp_path)
    response = service.skill_runtime_codex_call(
        skill,
        SkillCodexRequest(prompt="Track this call.", model="gpt-test"),
        internet_access=False,
    )
    service.skill_runtime_codex_call(
        skill,
        SkillCodexRequest(prompt="Track the second call.", model="gpt-test"),
        internet_access=False,
    )
    db_session.refresh(run)

    assert response["response"] == "Tracked response."
    assert len(run.codex_invocations_json) == 2
    assert run.codex_invocations_json[0]["adapter"] == "test_runtime_adapter"
    assert run.codex_invocations_json[0]["model"] == "gpt-test"
    assert run.input_tokens == 240
    assert run.cached_input_tokens == 40
    assert run.output_tokens == 60
    assert run.reasoning_output_tokens == 20
    assert run.total_tokens == 300


def test_failed_skill_runtime_codex_call_is_recorded_on_the_active_run(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_skill(db_session)
    run = SkillRun(
        skill_id=skill.id,
        status="running",
        input_json={},
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.commit()

    service = CodexService(db_session, adapter=FailingCodexAdapter(), project_root=tmp_path)
    with pytest.raises(CodexGenerationError, match="account quota is exhausted"):
        service.skill_runtime_codex_call(
            skill,
            SkillCodexRequest(prompt="Fail this call."),
            internet_access=False,
        )
    db_session.refresh(run)

    assert len(run.codex_invocations_json) == 1
    invocation = run.codex_invocations_json[0]
    assert invocation["status"] == "failed"
    assert invocation["exit_code"] == 1
    assert invocation["error_type"] == "CodexCliExitError"
    assert invocation["error_message"] == "Codex CLI exited with code 1: ERROR: account quota is exhausted"
    assert invocation["stderr_tail"].endswith("ERROR: account quota is exhausted\n")
    assert run.error_message == (
        "Codex runtime call failed: Codex CLI exited with code 1: ERROR: account quota is exhausted"
    )
    assert run.total_tokens == 0
