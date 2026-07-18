import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import ApprovalRequest, Skill, SkillGenerationRequest
from app.services.build_dependency_service import BuildDependencyError, BuildDependencyService
from app.services.dependency_environment import build_dependency_environment
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


def approved_build(db_session: Session, tmp_path: Path) -> tuple[SkillGenerationRequest, Skill, Path]:
    skill_dir = tmp_path / "skills" / "proposed" / "dependency_skill"
    skill_dir.mkdir(parents=True)
    skill = Skill(
        name="dependency_skill",
        description="Uses an approved dependency.",
        runtime="function",
        status="building",
        risk_level="medium",
        manifest_path="skills/proposed/dependency_skill/manifest.json",
        enabled=False,
    )
    generation_request = SkillGenerationRequest(
        user_message="Build a dependency skill.",
        proposed_skill_name=skill.name,
        proposed_display_name="Dependency Skill",
        plan_json={"skill_name": skill.name},
        requested_permissions_json={},
        requested_dependencies_json=["demo-package>=1"],
        requested_network_domains_json=[],
        risk_level="medium",
        status="approved",
        proposed_skill=skill,
    )
    db_session.add_all([skill, generation_request])
    db_session.flush()
    db_session.add(
        ApprovalRequest(
            generation_request_id=generation_request.id,
            request_scope="build_time",
            request_type="generation",
            risk_level="medium",
            requested_permissions_json={},
            requested_dependencies_json=["demo-package>=1"],
            requested_network_domains_json=[],
            requested_filesystem_json={},
            reason_json={},
            reason="approved",
            user_explanation="approved",
            status="approved",
        )
    )
    db_session.commit()
    return generation_request, skill, skill_dir


def permission_plan() -> dict[str, object]:
    return {
        "build_time": {"dependencies": ["pytest", "requests"]},
        "runtime": {"dependencies": ["demo-package>=1"]},
    }


def write_distribution(target: Path, name: str = "demo-package", version: str = "1.2") -> None:
    metadata_dir = target / f"{name.replace('-', '_')}-{version}.dist-info"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


def test_provisions_approved_runtime_dependency_once_before_build(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_request, skill, skill_dir = approved_build(db_session, tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        target = Path(command[command.index("--target") + 1])
        write_distribution(target)
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr("app.services.build_dependency_service.subprocess.run", fake_run)
    service = BuildDependencyService(db_session, project_root=tmp_path)

    first = service.provision(generation_request, skill, permission_plan())
    second = service.provision(generation_request, skill, permission_plan())

    assert len(calls) == 1
    assert calls[0][:5] == [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    assert str(skill_dir / ".deps") != calls[0][calls[0].index("--target") + 1]
    assert (skill_dir / ".deps" / ".personal-agent-dependencies.json").is_file()
    assert first.installed_runtime_dependencies == ["demo-package>=1"]
    assert second.reused_runtime_dependencies is True


def test_rejects_dependency_missing_from_approved_request(
    db_session: Session,
    tmp_path: Path,
) -> None:
    generation_request, skill, _skill_dir = approved_build(db_session, tmp_path)
    approval = generation_request.approval_requests[0]
    approval.requested_dependencies_json = []
    db_session.commit()

    with pytest.raises(BuildDependencyError, match="approval is missing for: demo-package>=1"):
        BuildDependencyService(db_session, project_root=tmp_path).provision(
            generation_request,
            skill,
            permission_plan(),
        )


def test_build_time_approval_includes_runtime_and_build_only_dependencies(db_session: Session) -> None:
    generation_request = SkillGenerationRequest(
        user_message="Build with two dependencies.",
        proposed_skill_name="approval_dependencies",
        proposed_display_name="Approval Dependencies",
        plan_json={
            "skill_name": "approval_dependencies",
            "permission_plan": {
                "build_time": {"dependencies": ["build-helper>=2"]},
                "runtime": {"dependencies": ["runtime-helper>=1"]},
            },
        },
        requested_permissions_json={},
        requested_dependencies_json=["runtime-helper>=1"],
        requested_network_domains_json=[],
        risk_level="medium",
        status="awaiting_approval",
    )
    db_session.add(generation_request)
    db_session.commit()

    request = PermissionService(db_session).create_build_time_request(generation_request)

    assert request.requested_dependencies_json == ["runtime-helper>=1", "build-helper>=2"]
    assert "provision" in request.reason_json["approval_means"]
    assert "installing packages" not in request.reason_json["approval_does_not_mean"]


def test_stops_when_approved_dependency_installation_fails(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_request, skill, _skill_dir = approved_build(db_session, tmp_path)

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="package unavailable")

    monkeypatch.setattr("app.services.build_dependency_service.subprocess.run", fake_run)

    with pytest.raises(BuildDependencyError, match="package unavailable"):
        BuildDependencyService(db_session, project_root=tmp_path).provision(
            generation_request,
            skill,
            permission_plan(),
        )


def test_stops_when_installed_dependency_cannot_be_verified(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_request, skill, _skill_dir = approved_build(db_session, tmp_path)

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="installed", stderr="")

    monkeypatch.setattr("app.services.build_dependency_service.subprocess.run", fake_run)

    with pytest.raises(BuildDependencyError, match="could not be verified"):
        BuildDependencyService(db_session, project_root=tmp_path).provision(
            generation_request,
            skill,
            permission_plan(),
        )


def test_stops_when_dependency_installation_times_out(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_request, skill, _skill_dir = approved_build(db_session, tmp_path)

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr("app.services.build_dependency_service.subprocess.run", fake_run)

    with pytest.raises(BuildDependencyError, match="Could not provision the controlled build environment"):
        BuildDependencyService(db_session, project_root=tmp_path).provision(
            generation_request,
            skill,
            permission_plan(),
        )


def test_build_environment_uses_provisioned_dependencies_and_backend_python(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    (skill_dir / ".deps").mkdir(parents=True)
    (skill_dir / ".build-deps").mkdir()

    env = build_dependency_environment(skill_dir)

    python_paths = env["PYTHONPATH"].split(";" if sys.platform == "win32" else ":")
    assert python_paths[:2] == [str(skill_dir / ".deps"), str(skill_dir / ".build-deps")]
    assert Path(env["PERSONAL_AGENT_BUILD_PYTHON"]) == Path(sys.executable).resolve()
    assert Path(env["PATH"].split(";" if sys.platform == "win32" else ":")[0]) == Path(sys.executable).resolve().parent
