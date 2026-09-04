import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Skill, SkillVersion
from app.services.permission_service import PermissionService
from app.services.skill_graph_service import SkillGraphError, SkillGraphService


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def add_function(
    db: Session,
    project_root: Path,
    name: str,
    *,
    children: list[str] | None = None,
    network: list[str] | None = None,
    enabled: bool = True,
) -> Skill:
    skill_dir = project_root / "skills" / "installed" / name / "versions" / "v1"
    (skill_dir / "tests").mkdir(parents=True)
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "name": name,
        "description": f"{name} function",
        "runtime": "function",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object", "additionalProperties": True},
        "requires_invocation_approval": False,
        "function_requirements": children or [],
        "integration_requirements": [],
        "dependencies": [],
        "permissions": {
            "network": network or [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
    }
    manifest_path = skill_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('{}')\n", encoding="utf-8")
    (skill_dir / "tests" / "test_skill.py").write_text(
        "def test_ok(): assert True\n",
        encoding="utf-8",
    )
    skill = Skill(
        name=name,
        description=manifest["description"],
        runtime="function",
        status="installed",
        risk_level="low",
        manifest_path=manifest_path.relative_to(project_root).as_posix(),
        installed_path=skill_dir.relative_to(project_root).as_posix(),
        input_schema_json=manifest["input_schema"],
        output_schema_json=manifest["output_schema"],
        function_requirements_json=children or [],
        enabled=enabled,
    )
    db.add(skill)
    db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version="v1",
        status="active",
        folder_path=skill.installed_path,
        code_snapshot_path=skill.installed_path,
        manifest_json=manifest,
        permission_fingerprint="test",
        validation_status="passed",
        test_status="passed",
    )
    db.add(version)
    db.flush()
    skill.active_version_id = version.id
    db.commit()
    db.refresh(skill)
    return skill


def test_parent_inherits_child_risk_and_permissions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    add_function(db_session, tmp_path, "network_child", network=["example.com"])
    parent = add_function(db_session, tmp_path, "parent", children=["network_child"])

    contract = SkillGraphService(db_session, project_root=tmp_path).effective_contract(parent)

    assert contract.risk_level == "medium"
    assert contract.permissions["network"] == ["example.com"]
    assert contract.descendants == ("network_child",)
    request = PermissionService(db_session, project_root=tmp_path).create_runtime_request(parent)
    assert request.risk_level == "medium"
    assert request.requested_permissions_json == contract.permissions


def test_disabled_and_deleted_children_propagate_availability(
    tmp_path: Path,
    db_session: Session,
) -> None:
    child = add_function(db_session, tmp_path, "child", enabled=False)
    parent = add_function(db_session, tmp_path, "parent", children=["child"])
    graph = SkillGraphService(db_session, project_root=tmp_path)

    disabled = graph.effective_contract(parent)
    assert disabled.availability == "disabled"

    db_session.query(SkillVersion).filter(SkillVersion.skill_id == child.id).delete(
        synchronize_session=False
    )
    db_session.query(Skill).filter(Skill.id == child.id).delete(synchronize_session=False)
    db_session.commit()
    deleted = graph.effective_contract(parent)
    assert deleted.availability == "error"
    assert "missing or deleted" in deleted.availability_reasons[0]


def test_function_graph_cycle_fails_strict_validation(
    tmp_path: Path,
    db_session: Session,
) -> None:
    first = add_function(db_session, tmp_path, "first", children=["second"])
    add_function(db_session, tmp_path, "second", children=["first"])

    with pytest.raises(SkillGraphError, match="cycle"):
        SkillGraphService(db_session, project_root=tmp_path).effective_contract(
            first,
            strict=True,
        )
