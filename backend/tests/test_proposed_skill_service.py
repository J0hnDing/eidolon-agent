import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill
from app.routers.skills import run_skill as run_skill_route
from app.schemas.skill_run import SkillRunRequest
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService


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


@pytest.fixture
def service(tmp_path: Path, db_session: Session) -> ProposedSkillService:
    return ProposedSkillService(db_session, project_root=tmp_path)


def test_create_proposed_instruction_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("sample_instruction", "instruction")
    skill_dir = service.proposed_dir("sample_instruction")

    assert skill.status == "proposed"
    assert skill.skill_type == "instruction"
    assert (skill_dir / "manifest.json").is_file()
    assert (skill_dir / "README.md").is_file()
    assert (skill_dir / "SKILL.md").is_file()
    assert not (skill_dir / "skill.py").exists()


def test_create_proposed_automation_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("sample_automation", "automation")
    skill_dir = service.proposed_dir("sample_automation")

    assert skill.status == "proposed"
    assert skill.skill_type == "automation"
    assert (skill_dir / "skill.py").is_file()
    assert (skill_dir / "tests" / "test_skill.py").is_file()


def test_create_proposed_hybrid_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("sample_hybrid", "hybrid")
    skill_dir = service.proposed_dir("sample_hybrid")

    assert skill.status == "proposed"
    assert skill.skill_type == "hybrid"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "skill.py").is_file()
    assert (skill_dir / "tests" / "test_skill.py").is_file()


def test_rejects_unsafe_skill_name(service: ProposedSkillService) -> None:
    with pytest.raises(ProposedSkillError, match="Skill name must match"):
        service.create_sample("../unsafe", "instruction")


def test_reads_allowed_proposed_skill_files(service: ProposedSkillService) -> None:
    skill = service.create_sample("readable_skill", "hybrid")

    files = service.read_skill_files(skill)
    paths = {file.path for file in files}

    assert paths == {"manifest.json", "README.md", "SKILL.md", "skill.py", "tests/test_skill.py"}


def test_blocks_reads_outside_skill_directory(service: ProposedSkillService) -> None:
    skill = service.create_sample("blocked_read", "instruction")

    with pytest.raises(ProposedSkillError, match="not readable"):
        service.read_skill_file(skill, "../AGENTS.md")


def test_validates_proposed_instruction_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("valid_instruction", "instruction")

    result = service.validate_proposed_skill(skill)

    assert result.ok is True
    assert result.skill_type == "instruction"
    assert result.manifest_valid is True
    assert result.tests_run is False


def test_validates_proposed_automation_with_passing_tests(service: ProposedSkillService) -> None:
    skill = service.create_sample("valid_automation", "automation")

    result = service.validate_proposed_skill(skill)

    assert result.ok is True
    assert result.skill_type == "automation"
    assert result.tests_run is True
    assert result.tests_passed is True


def test_validation_fails_when_automation_tests_fail(service: ProposedSkillService) -> None:
    skill = service.create_sample("failing_automation", "automation")
    skill_dir = service.proposed_dir("failing_automation")
    (skill_dir / "tests" / "test_skill.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    result = service.validate_proposed_skill(skill)

    assert result.ok is False
    assert result.tests_run is True
    assert result.tests_passed is False
    assert result.error_message == "Skill tests failed"


def test_installs_valid_proposed_instruction_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("install_instruction", "instruction")

    installed = service.install_proposed_skill(skill)

    assert installed.status == "installed"
    assert installed.skill_type == "instruction"
    assert installed.enabled is True
    assert service.installed_dir("install_instruction").is_dir()
    assert not service.proposed_dir("install_instruction").exists()


def test_installs_valid_proposed_automation_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("install_automation", "automation")

    installed = service.install_proposed_skill(skill)

    assert installed.status == "installed"
    assert installed.skill_type == "automation"
    assert installed.enabled is False
    assert installed.installed_path == "skills/installed/install_automation"
    assert service.installed_dir("install_automation").is_dir()
    assert not service.proposed_dir("install_automation").exists()


def test_refuses_to_install_invalid_manifest(service: ProposedSkillService) -> None:
    skill = service.create_sample("invalid_manifest", "automation")
    skill_dir = service.proposed_dir("invalid_manifest")
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ProposedSkillError, match="automation skills require entrypoint"):
        service.install_proposed_skill(skill)


def test_refuses_to_run_proposed_skills(db_session: Session, service: ProposedSkillService) -> None:
    skill = service.create_sample("proposed_run_block", "automation")

    with pytest.raises(HTTPException) as exc_info:
        run_skill_route(skill.id, SkillRunRequest(input={}), db_session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Only installed skills can be run"


def test_refuses_to_run_instruction_only_skills(db_session: Session, service: ProposedSkillService) -> None:
    skill = service.create_sample("instruction_run_block", "instruction")
    installed = service.install_proposed_skill(skill)

    with pytest.raises(HTTPException) as exc_info:
        run_skill_route(installed.id, SkillRunRequest(input={}), db_session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Instruction skills cannot be run"


def test_rejects_and_deletes_proposed_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("reject_me", "automation")
    skill_id = skill.id

    service.reject_proposed_skill(skill)

    assert service.db.get(Skill, skill_id) is None
    assert not service.proposed_dir("reject_me").exists()


def test_delete_removes_installed_skill_record_and_folder(
    db_session: Session,
    service: ProposedSkillService,
) -> None:
    skill = service.create_sample("delete_installed", "automation")
    installed = service.install_proposed_skill(skill)
    installed_id = installed.id

    service.delete_skill(installed)

    assert db_session.get(Skill, installed_id) is None
    assert not service.installed_dir("delete_installed").exists()


def test_sync_installed_from_filesystem_registers_hidden_installed_skill(
    service: ProposedSkillService,
) -> None:
    skill_dir = service.installed_dir("hidden_installed")
    skill_dir.mkdir(parents=True)
    manifest = {
        "name": "hidden_installed",
        "description": "Installed on disk but missing from the database.",
        "skill_type": "automation",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "risk_level": "low",
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "test",
        "enabled": False,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{}')\n", encoding="utf-8")
    (skill_dir / "tests").mkdir()
    (skill_dir / "tests" / "test_skill.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    service.sync_installed_from_filesystem()

    skill = service.db.scalar(select(Skill).where(Skill.name == "hidden_installed"))
    assert skill is not None
    assert skill.status == "installed"
    assert skill.installed_path == "skills/installed/hidden_installed"
