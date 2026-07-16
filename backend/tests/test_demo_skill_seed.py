from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill
from app.services.demo_skill_seed import register_personal_news_digest


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


def test_register_personal_news_digest_creates_installed_skill(db_session: Session) -> None:
    skill = register_personal_news_digest(db_session)

    assert skill.name == "personal_news_digest"
    assert skill.status == "installed"
    assert skill.enabled is True
    assert skill.instructions_path is None
    assert skill.installed_path == "skills/installed/personal_news_digest"
    assert skill.manifest_path == "skills/installed/personal_news_digest/manifest.json"


def test_register_personal_news_digest_updates_existing_skill(db_session: Session) -> None:
    original = Skill(
        name="personal_news_digest",
        description="Old",
        status="installed",
        risk_level="low",
        manifest_path="old/manifest.json",
        instructions_path="old.md",
        installed_path=None,
        enabled=False,
    )
    db_session.add(original)
    db_session.commit()

    skill = register_personal_news_digest(db_session)
    count = len(db_session.scalars(select(Skill).where(Skill.name == "personal_news_digest")).all())

    assert count == 1
    assert skill.id == original.id
    assert skill.status == "installed"
    assert skill.enabled is True
    assert skill.instructions_path is None
