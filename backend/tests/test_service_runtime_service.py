import json
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Skill, SkillRun, SkillVersion
from app.services.service_runtime_service import ServiceRuntimeService


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    Base.metadata.drop_all(engine)


class FakeRunner:
    def __init__(self, db: Session, output: dict[str, Any]) -> None:
        self.db = db
        self.output = output
        self.calls: list[Any] = []

    def run(self, skill_id: int, skill_dir: Path, input_json: dict[str, Any], context=None) -> SkillRun:
        self.calls.append(context)
        run = SkillRun(
            skill_id=skill_id,
            version_id=context.version_id,
            status="succeeded",
            input_json=input_json,
            output_json=self.output,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            exit_code=0,
            invocation_source=context.invocation_source,
            source_schedule_id=context.source_schedule_id,
            initiating_action=context.initiating_action,
            function_capability_token_hash=context.capability_token_hash,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run


def make_service(db: Session, project_root: Path) -> Skill:
    service_dir = project_root / "skills" / "installed" / "daily_report" / "versions" / "v1"
    (service_dir / "tests").mkdir(parents=True)
    manifest = {
        "manifest_version": 1,
        "name": "daily_report",
        "description": "Daily report service",
        "runtime": "service",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        "function_requirements": [],
        "integration_requirements": [],
        "dependencies": [],
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": {
            "type": "daily",
            "time": "08:00",
            "timezone": "America/Toronto",
            "input": {"topic": "AI"},
        },
    }
    (service_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (service_dir / "skill.py").write_text("print('{}')\n", encoding="utf-8")
    (service_dir / "tests" / "test_skill.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    skill = Skill(
        name="daily_report",
        description="Daily report service",
        runtime="service",
        status="installed",
        risk_level="low",
        manifest_path=(service_dir / "manifest.json").relative_to(project_root).as_posix(),
        installed_path=service_dir.relative_to(project_root).as_posix(),
        input_schema_json=manifest["input_schema"],
        output_schema_json=manifest["output_schema"],
        enabled=True,
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


def test_service_run_is_schedule_attributed_and_schema_validated(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = make_service(db_session, tmp_path)
    runner = FakeRunner(db_session, {"result": "ready"})
    runtime = ServiceRuntimeService(
        db_session,
        project_root=tmp_path,
        runner_factory=lambda _db: runner,
    )

    scheduled_for_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    run = runtime.run(
        skill,
        {"topic": "AI"},
        schedule_id=7,
        schedule_occurrence_key="a" * 64,
        scheduled_for_at=scheduled_for_at,
        schedule_trigger="startup_catch_up",
    )

    assert run.status == "succeeded"
    assert run.invocation_source == "schedule"
    assert run.source_schedule_id == 7
    assert runner.calls[0].capability_token is not None
    assert runner.calls[0].schedule_occurrence_key == "a" * 64
    assert runner.calls[0].scheduled_for_at == scheduled_for_at
    assert runner.calls[0].schedule_trigger == "startup_catch_up"


def test_service_run_blocks_bad_input_and_fails_bad_output(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = make_service(db_session, tmp_path)
    runner = FakeRunner(db_session, {"unexpected": True})
    runtime = ServiceRuntimeService(
        db_session,
        project_root=tmp_path,
        runner_factory=lambda _db: runner,
    )

    blocked = runtime.run(skill, {"topic": 5}, schedule_id=7)
    failed = runtime.run(skill, {"topic": "AI"}, schedule_id=7)

    assert blocked.status == "blocked"
    assert "Service input does not match" in blocked.error_message
    assert failed.status == "failed"
    assert "Service output does not match" in failed.error_message
