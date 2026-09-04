from collections.abc import Generator

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill, SkillSchedule
from app.routers.skills import _disable_dependents, delete_skill, list_skills, router, update_skill
from app.schemas.skill import SkillUpdate


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


def make_skill(db: Session, *, status: str = "installed") -> Skill:
    skill = Skill(
        name=f"skill_{status}",
        description="Router safety test",
        status=status,
        risk_level="low",
        manifest_path=f"skills/proposed/skill_{status}/manifest.json",
        enabled=False,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def test_bare_skill_creation_route_is_not_exposed() -> None:
    assert not any(route.path == "/skills" and "POST" in route.methods for route in router.routes)
    assert not any(route.path == "/skills/proposed/sample" for route in router.routes)


def test_list_skills_does_not_reconcile_the_filesystem(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_skill(db_session)
    monkeypatch.setattr(
        "app.services.proposed_skill_service.ProposedSkillService.sync_installed_from_filesystem",
        lambda *_args, **_kwargs: pytest.fail("GET /skills must not reconcile the filesystem"),
    )

    skills = list_skills(db_session)

    assert len(skills) == 1


def test_skill_update_rejects_platform_owned_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SkillUpdate(status="installed")


def test_skill_update_only_toggles_installed_skill(db_session: Session) -> None:
    installed = make_skill(db_session)

    updated = update_skill(installed.id, SkillUpdate(enabled=True), db_session)

    assert updated.enabled is True


def test_skill_update_cannot_enable_proposed_skill(db_session: Session) -> None:
    proposed = make_skill(db_session, status="proposed")

    with pytest.raises(HTTPException, match="Only installed skills") as exc_info:
        update_skill(proposed.id, SkillUpdate(enabled=True), db_session)

    assert exc_info.value.status_code == 409


def test_disabling_child_persists_disabled_state_on_parent_functions(
    db_session: Session,
) -> None:
    child = make_skill(db_session)
    child.name = "child"
    child.enabled = True
    parent = Skill(
        name="parent",
        description="Parent function",
        runtime="function",
        status="installed",
        risk_level="low",
        manifest_path="skills/installed/parent/versions/v1/manifest.json",
        installed_path="skills/installed/parent/versions/v1",
        function_requirements_json=[child.name],
        enabled=True,
    )
    db_session.add(parent)
    db_session.commit()

    _disable_dependents(child, db_session, object())  # type: ignore[arg-type]

    assert parent.enabled is False


def test_delete_skill_removes_live_schedule_job_and_row(db_session: Session) -> None:
    skill = make_skill(db_session)
    schedule = SkillSchedule(
        skill_id=skill.id,
        name="Delete with skill",
        status="active",
        schedule_type="daily",
        schedule_json={"type": "daily", "time": "09:00", "timezone": "America/Toronto", "input": {}},
        input_json={},
        timezone="America/Toronto",
    )
    db_session.add(schedule)
    db_session.commit()
    schedule_id = schedule.id

    class FakeSchedulerService:
        removed: list[int] = []

        def remove_job(self, value: int) -> None:
            self.removed.append(value)

    scheduler = FakeSchedulerService()
    delete_skill(skill.id, db_session, scheduler)  # type: ignore[arg-type]

    assert scheduler.removed == [schedule_id]
    assert db_session.get(SkillSchedule, schedule_id) is None
