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


SKILL_RELATIVE_PATH = "skills/installed/simple_calculator_tool"
CALCULATOR_SKILL_SOURCE = """
import json
import sys


def calculate(operation, left, right):
    if operation == "add":
        return {"result": left + right, "error": None}
    if operation == "subtract":
        return {"result": left - right, "error": None}
    if operation == "multiply":
        return {"result": left * right, "error": None}
    if operation == "divide":
        if right == 0:
            return {"result": None, "error": "Division by zero is not allowed."}
        return {"result": left / right, "error": None}
    return {"result": None, "error": f"Unsupported operation: {operation}"}


def main():
    payload = json.loads(sys.stdin.read() or "{}")
    output = calculate(payload.get("operation"), payload.get("left"), payload.get("right"))
    print(json.dumps(output))
    return 0 if output.get("error") is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
""".strip()


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
def test_project_root(tmp_path: Path) -> Path:
    skill_dir = tmp_path / SKILL_RELATIVE_PATH
    tests_dir = skill_dir / "tests"
    tests_dir.mkdir(parents=True)
    manifest = {
        "name": "simple_calculator_tool",
        "description": "Temporary calculator tool for tests.",
        "skill_type": "automation",
        "interface_type": "tool",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": None,
        "output_schema": None,
        "tool_ui_schema": {
            "title": "Calculator",
            "fields": [{"name": "operation", "label": "Operation", "type": "text"}],
        },
        "risk_level": "low",
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": [],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "test",
        "enabled": False,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "README.md").write_text("# Test calculator\n", encoding="utf-8")
    (skill_dir / "skill.py").write_text(CALCULATOR_SKILL_SOURCE, encoding="utf-8")
    (tests_dir / "test_skill.py").write_text("def test_placeholder():\n    assert True\n", encoding="utf-8")
    return tmp_path


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
        manifest_path=f"{SKILL_RELATIVE_PATH}/manifest.json",
        tool_ui_schema_json={
            "title": "Calculator",
            "fields": [{"name": "expression", "label": "Expression", "type": "text"}],
        },
        installed_path=SKILL_RELATIVE_PATH,
        enabled=enabled,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def approve_runtime(db: Session, skill: Skill, project_root: Path) -> None:
    permission_service = PermissionService(db, project_root=project_root)
    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)


def test_tools_list_only_installed_enabled_runnable_tool_skills(db_session: Session, test_project_root: Path) -> None:
    included = create_skill(db_session, name="included_tool")
    create_skill(db_session, name="instruction_tool", skill_type="instruction")
    create_skill(db_session, name="disabled_tool", enabled=False)
    create_skill(db_session, name="proposed_tool", status="proposed")
    create_skill(db_session, name="chat_skill", interface_type="chat")
    approve_runtime(db_session, included, test_project_root)

    tools = list_tools(db_session)
    names = {tool.skill.name for tool in tools}

    assert "included_tool" in names
    assert "instruction_tool" not in names
    assert "disabled_tool" not in names
    assert "proposed_tool" not in names
    assert "chat_skill" not in names


def test_get_tool_returns_single_tool_with_ui_schema(db_session: Session, test_project_root: Path) -> None:
    skill = create_skill(db_session, name="calculator_card")
    approve_runtime(db_session, skill, test_project_root)

    tool = get_tool(skill.id, db_session)

    assert tool.skill.name == "calculator_card"
    assert tool.skill.tool_ui_schema_json["title"] == "Calculator"


def test_installed_sync_preserves_user_enabled_state(db_session: Session, test_project_root: Path) -> None:
    skill = create_skill(db_session, name="simple_calculator_tool", enabled=True)
    approve_runtime(db_session, skill, test_project_root)

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
    test_project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_skill(db_session, name="approved_tool")
    approve_runtime(db_session, skill, test_project_root)

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


def test_calculator_tool_outputs_result(test_project_root: Path) -> None:
    result = run_calculator(test_project_root, {"operation": "add", "left": 2, "right": 3})

    assert result == {"result": 5, "error": None}


@pytest.mark.parametrize(
    "expression",
    ["power", "__import__", "open"],
)
def test_calculator_tool_rejects_unsafe_operations(expression: str, test_project_root: Path) -> None:
    result = run_calculator(test_project_root, {"operation": expression, "left": 2, "right": 3}, expect_success=False)

    assert result["result"] is None
    assert result["error"]


def run_calculator(project_root: Path, payload: dict, *, expect_success: bool = True) -> dict:
    calculator_dir = project_root / SKILL_RELATIVE_PATH
    result = subprocess.run(
        [sys.executable, str(calculator_dir / "skill.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        shell=False,
    )
    assert result.returncode == (0 if expect_success else 1)
    return json.loads(result.stdout)
