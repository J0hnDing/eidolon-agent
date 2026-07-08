import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill
from app.routers import skills as skills_router
from app.routers.skills import call_codex_for_skill
from app.schemas.skill_codex import SkillCodexRequest
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
        "skill_type": "automation",
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
        skill_type="automation",
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
