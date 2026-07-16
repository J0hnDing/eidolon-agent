from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill, SkillOperationLock, SkillRun
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard


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


def create_skill(db: Session, name: str = "guarded_skill") -> Skill:
    skill = Skill(
        name=name,
        description="Guard test skill",
        status="installed",
        risk_level="low",
        manifest_path=f"skills/installed/{name}/manifest.json",
        installed_path=f"skills/installed/{name}",
        enabled=True,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def test_only_one_operation_lock_per_skill(db_session: Session) -> None:
    skill = create_skill(db_session)
    guard = SkillOperationGuard(db_session)

    first = guard.acquire(skill, "run")

    assert first.operation == "run"
    with pytest.raises(SkillOperationConflict, match="busy with run"):
        guard.acquire(skill, "run")

    guard.release(skill.id)
    second = guard.acquire(skill, "run")

    assert second.operation == "run"


@pytest.mark.parametrize("operation", ["install", "repair", "delete"])
def test_mutating_operations_block_when_run_is_active(db_session: Session, operation: str) -> None:
    skill = create_skill(db_session)
    db_session.add(SkillRun(skill_id=skill.id, status="running", input_json={}))
    db_session.commit()

    with pytest.raises(SkillOperationConflict, match="active run"):
        SkillOperationGuard(db_session).acquire(skill, operation)


def test_run_blocks_when_skill_is_being_installed(db_session: Session) -> None:
    skill = create_skill(db_session)
    guard = SkillOperationGuard(db_session)
    guard.acquire(skill, "install")

    with pytest.raises(SkillOperationConflict, match="busy with install"):
        guard.acquire(skill, "run")


def test_delete_refuses_active_run_and_keeps_record(
    tmp_path: Path,
    db_session: Session,
) -> None:
    service = ProposedSkillService(db_session, project_root=tmp_path)
    skill = service.create_sample("delete_guard")
    db_session.add(SkillRun(skill_id=skill.id, status="running", input_json={}))
    db_session.commit()

    with pytest.raises(ProposedSkillError, match="active run"):
        service.delete_skill(skill)

    assert db_session.get(Skill, skill.id) is not None
    assert service.proposed_dir("delete_guard").exists()
    assert db_session.get(SkillOperationLock, skill.id) is None
