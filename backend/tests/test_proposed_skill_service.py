import json
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import ApprovalRequest, Skill, SkillGenerationRequest, SkillSchedule
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


def test_create_proposed_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("sample_skill")
    skill_dir = service.proposed_dir("sample_skill")

    assert skill.status == "proposed"
    assert (skill_dir / "manifest.json").is_file()
    assert (skill_dir / "README.md").is_file()
    assert (skill_dir / "skill.py").is_file()
    assert (skill_dir / "tests" / "test_skill.py").is_file()


def test_rejects_unsafe_skill_name(service: ProposedSkillService) -> None:
    with pytest.raises(ProposedSkillError, match="Skill name must match"):
        service.create_sample("../unsafe")


def test_prepare_generation_workspace_stages_existing_tree_before_cleanup(
    service: ProposedSkillService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_workspace = service.proposed_dir("replace_build")
    blocked_cache = old_workspace / ".pytest_cache"
    blocked_cache.mkdir(parents=True)
    (blocked_cache / "nodeids").write_text("sandbox-owned", encoding="utf-8")
    monkeypatch.setattr(service, "_remove_tree_best_effort", lambda _path: None)

    workspace = service.prepare_generation_workspace("replace_build")

    assert workspace == old_workspace
    assert workspace.is_dir()
    assert list(workspace.iterdir()) == []
    staged = list((service.project_root / "runtime" / "file_trash").iterdir())
    assert len(staged) == 1
    assert (staged[0] / ".pytest_cache" / "nodeids").read_text(encoding="utf-8") == "sandbox-owned"


def test_reads_allowed_proposed_skill_files(service: ProposedSkillService) -> None:
    skill = service.create_sample("readable_skill")

    files = service.read_skill_files(skill)
    paths = {file.path for file in files}

    assert paths == {"manifest.json", "README.md", "skill.py", "tests/test_skill.py"}


def test_validates_skill_with_optional_instructions_file(service: ProposedSkillService) -> None:
    skill = service.create_sample("skill_with_instructions")
    skill_dir = service.proposed_dir("skill_with_instructions")
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["instructions_path"] = "SKILL.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("# Optional instructions\n", encoding="utf-8")

    result = service.validate_proposed_skill(skill)

    assert result.ok is True


def test_blocks_reads_outside_skill_directory(service: ProposedSkillService) -> None:
    skill = service.create_sample("blocked_read")

    with pytest.raises(ProposedSkillError, match="not readable"):
        service.read_skill_file(skill, "../AGENTS.md")


def test_validates_proposed_skill_with_passing_tests(service: ProposedSkillService) -> None:
    skill = service.create_sample("valid_skill")

    result = service.validate_proposed_skill(skill)

    assert result.ok is True
    assert result.tests_run is True
    assert result.tests_passed is True


def test_validation_blocks_unapproved_dependencies(service: ProposedSkillService) -> None:
    skill = service.create_sample("dependency_without_approval")
    skill_dir = service.proposed_dir("dependency_without_approval")
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"] = ["requests"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = service.validate_proposed_skill(skill)

    assert result.ok is False
    assert result.tests_run is False
    assert result.error_message == "Skill dependency installation failed"
    assert "Build-time dependency approval is missing for: requests" in result.stderr


def test_validation_installs_approved_dependencies_locally(
    monkeypatch: pytest.MonkeyPatch,
    service: ProposedSkillService,
    db_session: Session,
) -> None:
    skill = service.create_sample("dependency_with_approval")
    skill_dir = service.proposed_dir("dependency_with_approval")
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"] = ["requests"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    generation_request = SkillGenerationRequest(
        user_message="Create a web scraper skill.",
        proposed_skill_name=skill.name,
        proposed_display_name="Dependency With Approval",
        plan_json={
            "skill_name": skill.name,
            "requested_dependencies": ["requests"],
            "requested_permissions": manifest["permissions"],
        },
        requested_permissions_json=manifest["permissions"],
        requested_dependencies_json=["requests"],
        requested_network_domains_json=[],
        risk_level="medium",
        status="approved",
        proposed_skill_id=skill.id,
    )
    db_session.add(generation_request)
    db_session.add(
        ApprovalRequest(
            generation_request=generation_request,
            request_scope="build_time",
            request_type="generation",
            risk_level="medium",
            requested_permissions_json={},
            requested_dependencies_json=["requests"],
            requested_network_domains_json=[],
            requested_filesystem_json={},
            reason_json={},
            reason="approved",
            user_explanation="approved",
            status="approved",
        )
    )
    db_session.commit()
    calls: list[dict[str, Any]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, "kwargs": kwargs})
        if "pip" in command:
            return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="tests passed", stderr="")

    monkeypatch.setattr("app.services.proposed_skill_service.subprocess.run", fake_run)

    result = service.validate_proposed_skill(skill)

    assert result.ok is True
    pip_call = calls[0]
    assert pip_call["command"][:5] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
    ]
    assert "--target" in pip_call["command"]
    assert str(skill_dir / ".deps") in pip_call["command"]
    test_call = calls[1]
    assert str(skill_dir / ".deps") in test_call["kwargs"]["env"]["PYTHONPATH"]


def test_validation_fails_when_skill_tests_fail(service: ProposedSkillService) -> None:
    skill = service.create_sample("failing_skill")
    skill_dir = service.proposed_dir("failing_skill")
    (skill_dir / "tests" / "test_skill.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    result = service.validate_proposed_skill(skill)

    assert result.ok is False
    assert result.tests_run is True
    assert result.tests_passed is False
    assert result.error_message == "Skill tests failed"


def test_installs_valid_proposed_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("install_skill")

    installed = service.install_proposed_skill(skill)

    assert installed.status == "installed"
    assert installed.enabled is False
    assert installed.installed_path == "skills/installed/install_skill/versions/v1"
    assert service.installed_dir("install_skill").is_dir()
    assert (service.installed_dir("install_skill") / "versions" / "v1").is_dir()
    assert installed.active_version_id is not None
    assert not service.proposed_dir("install_skill").exists()


def test_install_recovers_partial_folder_and_does_not_fail_on_deferred_trash_cleanup(
    service: ProposedSkillService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = service.create_sample("recover_install")
    partial_dir = service.installed_dir(skill.name)
    partial_dir.mkdir(parents=True)
    (partial_dir / "partial.txt").write_text("incomplete", encoding="utf-8")
    monkeypatch.setattr(service, "_remove_tree_best_effort", lambda _path: None)

    installed = service.install_proposed_skill(skill)

    assert installed.status == "installed"
    assert not service.proposed_dir(skill.name).exists()
    assert not (partial_dir / "partial.txt").exists()
    assert (partial_dir / "versions" / "v1" / "manifest.json").is_file()
    assert list((service.project_root / "runtime" / "file_trash").iterdir())


def test_install_is_idempotent_after_success(service: ProposedSkillService) -> None:
    skill = service.create_sample("idempotent_install")
    installed = service.install_proposed_skill(skill)

    retried = service.install_proposed_skill(installed)

    assert retried.id == installed.id
    assert retried.status == "installed"


def test_install_registers_manifest_declared_schedule(
    service: ProposedSkillService,
    db_session: Session,
) -> None:
    skill = service.create_sample("install_scheduled")
    skill_dir = service.proposed_dir("install_scheduled")
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schedule"] = {
        "type": "weekly",
        "day": "monday",
        "time": "09:00",
        "timezone": "America/Toronto",
        "input": {"limit": 10},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    installed = service.install_proposed_skill(skill)

    schedule = db_session.scalar(select(SkillSchedule).where(SkillSchedule.skill_id == installed.id))
    assert schedule is not None
    assert schedule.status == "pending"
    assert schedule.schedule_type == "weekly"
    assert schedule.schedule_json["day"] == "monday"
    assert schedule.input_json == {"limit": 10}
    approval = db_session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.skill_id == installed.id)
        .where(ApprovalRequest.schedule_id == schedule.id)
        .where(ApprovalRequest.request_type == "schedule")
    )
    assert approval is not None
    assert approval.status == "pending"


def test_refuses_to_install_invalid_manifest(service: ProposedSkillService) -> None:
    skill = service.create_sample("invalid_manifest")
    skill_dir = service.proposed_dir("invalid_manifest")
    manifest_path = skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ProposedSkillError, match="skills require entrypoint"):
        service.install_proposed_skill(skill)


def test_validation_rejects_manifest_name_drift(service: ProposedSkillService) -> None:
    skill = service.create_sample("stable_name")
    manifest_path = service.proposed_dir(skill.name) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "renamed_by_agent"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = service.validate_proposed_skill(skill)

    assert result.ok is False
    assert result.manifest_valid is False
    assert "controlled skill name" in (result.error_message or "")


def test_refuses_to_run_proposed_skills(db_session: Session, service: ProposedSkillService) -> None:
    skill = service.create_sample("proposed_run_block")

    with pytest.raises(HTTPException) as exc_info:
        run_skill_route(skill.id, SkillRunRequest(input={}), db_session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Only installed skills can be run"


def test_rejects_and_deletes_proposed_skill(service: ProposedSkillService) -> None:
    skill = service.create_sample("reject_me")
    skill_id = skill.id

    service.reject_proposed_skill(skill)

    assert service.db.get(Skill, skill_id) is None
    assert not service.proposed_dir("reject_me").exists()


def test_delete_removes_installed_skill_record_and_folder(
    db_session: Session,
    service: ProposedSkillService,
) -> None:
    skill = service.create_sample("delete_installed")
    installed = service.install_proposed_skill(skill)
    installed_id = installed.id

    service.delete_skill(installed)

    assert db_session.get(Skill, installed_id) is None
    assert not service.installed_dir("delete_installed").exists()


def test_delete_commits_after_staging_even_when_trash_cleanup_is_deferred(
    db_session: Session,
    service: ProposedSkillService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = service.create_sample("delete_locked")
    installed = service.install_proposed_skill(skill)
    installed_id = installed.id
    monkeypatch.setattr(service, "_remove_tree_best_effort", lambda _path: None)

    service.delete_skill(installed)

    assert db_session.get(Skill, installed_id) is None
    assert not service.installed_dir("delete_locked").exists()
    assert list((service.project_root / "runtime" / "file_trash").iterdir())


def test_delete_removes_skill_schedules(
    db_session: Session,
    service: ProposedSkillService,
) -> None:
    skill = service.create_sample("delete_scheduled")
    installed = service.install_proposed_skill(skill)
    schedule = SkillSchedule(
        skill_id=installed.id,
        name="Delete with skill",
        status="pending",
        schedule_type="daily",
        schedule_json={"type": "daily", "time": "09:00", "timezone": "America/Toronto", "input": {}},
        input_json={},
        timezone="America/Toronto",
    )
    db_session.add(schedule)
    db_session.commit()
    schedule_id = schedule.id

    service.delete_skill(installed)

    assert db_session.get(SkillSchedule, schedule_id) is None


def test_sync_installed_from_filesystem_registers_hidden_installed_skill(
    service: ProposedSkillService,
) -> None:
    skill_dir = service.installed_dir("hidden_installed")
    skill_dir.mkdir(parents=True)
    manifest = {
        "name": "hidden_installed",
        "description": "Installed on disk but missing from the database.",
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
