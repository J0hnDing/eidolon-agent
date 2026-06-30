import json
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill, SkillRun
from app.routers.tools import get_tool, list_tools, run_tool
from app.schemas.skill_run import SkillRunRequest
from app.services.permission_service import PermissionService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALCULATOR_DIR = PROJECT_ROOT / "skills" / "installed" / "simple_calculator_tool"


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


def create_skill(
    db: Session,
    *,
    name: str,
    skill_type: str = "automation",
    interface_type: str = "tool",
    status: str = "installed",
    enabled: bool = True,
) -> Skill:
    skill = Skill(
        name=name,
        description=f"{name} description",
        skill_type=skill_type,
        interface_type=interface_type,
        status=status,
        risk_level="low",
        manifest_path="skills/installed/simple_calculator_tool/manifest.json",
        tool_ui_schema_json={
            "title": "Calculator",
            "fields": [{"name": "expression", "label": "Expression", "type": "text"}],
        },
        installed_path="skills/installed/simple_calculator_tool",
        enabled=enabled,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def approve_runtime(db: Session, skill: Skill) -> None:
    permission_service = PermissionService(db)
    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)


def test_tools_list_only_installed_enabled_runnable_tool_skills(db_session: Session) -> None:
    included = create_skill(db_session, name="included_tool")
    create_skill(db_session, name="instruction_tool", skill_type="instruction")
    create_skill(db_session, name="disabled_tool", enabled=False)
    create_skill(db_session, name="proposed_tool", status="proposed")
    create_skill(db_session, name="chat_skill", interface_type="chat")
    approve_runtime(db_session, included)

    tools = list_tools(db_session)
    names = {tool.skill.name for tool in tools}

    assert "included_tool" in names
    assert "instruction_tool" not in names
    assert "disabled_tool" not in names
    assert "proposed_tool" not in names
    assert "chat_skill" not in names


def test_get_tool_returns_single_tool_with_ui_schema(db_session: Session) -> None:
    skill = create_skill(db_session, name="calculator_card")
    approve_runtime(db_session, skill)

    tool = get_tool(skill.id, db_session)

    assert tool.skill.name == "calculator_card"
    assert tool.skill.tool_ui_schema_json["title"] == "Calculator"


def test_installed_sync_preserves_user_enabled_state(db_session: Session) -> None:
    skill = create_skill(db_session, name="simple_calculator_tool", enabled=True)
    approve_runtime(db_session, skill)

    tools = list_tools(db_session)

    assert "simple_calculator_tool" in {tool.skill.name for tool in tools}
    db_session.refresh(skill)
    assert skill.enabled is True


def test_tool_run_requires_runtime_permission_approval(db_session: Session) -> None:
    skill = create_skill(db_session, name="needs_permission")

    with pytest.raises(HTTPException) as exc_info:
        run_tool(skill.id, SkillRunRequest(input={"expression": "1+2"}), db_session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Runtime permissions have not been reviewed"


def test_tool_run_uses_safe_runner_after_permission_approval(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_skill(db_session, name="approved_tool")
    approve_runtime(db_session, skill)

    class FakeRunner:
        def run(self, skill_id, skill_dir, input_json):
            run = SkillRun(
                skill_id=skill_id,
                status="succeeded",
                input_json=input_json,
                output_json={"result": 3},
                stdout='{"result": 3}',
                stderr="",
                exit_code=0,
            )
            db_session.add(run)
            db_session.commit()
            db_session.refresh(run)
            return run

    monkeypatch.setattr("app.routers.tools.get_skill_runner", lambda db: FakeRunner())

    response = run_tool(skill.id, SkillRunRequest(input={"expression": "1+2"}), db_session)

    assert response.run.status == "succeeded"
    assert response.run.output_json == {"result": 3}


def test_tool_run_rejects_instruction_skill(db_session: Session) -> None:
    skill = create_skill(db_session, name="instruction_block", skill_type="instruction")

    with pytest.raises(HTTPException) as exc_info:
        run_tool(skill.id, SkillRunRequest(input={}), db_session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Instruction skills cannot be run as tools"


def test_calculator_tool_outputs_result() -> None:
    result = run_calculator({"operation": "add", "left": 2, "right": 3})

    assert result == {"result": 5, "error": None}


@pytest.mark.parametrize(
    "expression",
        ["power", "__import__", "open"],
    )
def test_calculator_tool_rejects_unsafe_operations(expression: str) -> None:
    result = run_calculator({"operation": expression, "left": 2, "right": 3}, expect_success=False)

    assert result["result"] is None
    assert result["error"]


def run_calculator(payload: dict, *, expect_success: bool = True) -> dict:
    result = subprocess.run(
        [sys.executable, str(CALCULATOR_DIR / "skill.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    assert result.returncode == (0 if expect_success else 1)
    return json.loads(result.stdout)
