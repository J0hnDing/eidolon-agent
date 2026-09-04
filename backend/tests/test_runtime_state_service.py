from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    IntegrationAuditRecord,
    McpAuditRecord,
    ScheduleOccurrence,
    Skill,
    SkillRun,
    SkillSchedule,
    SkillVersion,
    WebAppInstance,
)
from app.services.runtime_state_service import RuntimeStateService


def test_runtime_state_projects_only_active_records() -> None:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        function = Skill(
            name="active_function",
            description="Active function",
            runtime="function",
            status="installed",
            risk_level="low",
            manifest_path="skills/installed/active_function/manifest.json",
            enabled=True,
        )
        web_app = Skill(
            name="active_app",
            description="Active app",
            runtime="web_app",
            status="installed",
            risk_level="low",
            manifest_path="skills/installed/active_app/manifest.json",
            enabled=True,
        )
        service = Skill(
            name="active_service",
            description="Active service",
            runtime="service",
            status="installed",
            risk_level="low",
            manifest_path="skills/installed/active_service/manifest.json",
            enabled=True,
        )
        db.add_all([function, web_app, service])
        db.flush()
        function_version = SkillVersion(
            skill_id=function.id,
            version="v1",
            status="active",
            folder_path="skills/installed/active_function/versions/v1",
            manifest_json={},
            code_snapshot_path="skills/installed/active_function/versions/v1",
        )
        web_app_version = SkillVersion(
            skill_id=web_app.id,
            version="v1",
            status="active",
            folder_path="skills/installed/active_app/versions/v1",
            manifest_json={},
            code_snapshot_path="skills/installed/active_app/versions/v1",
        )
        db.add_all([function_version, web_app_version])
        db.flush()
        function.active_version_id = function_version.id
        web_app.active_version_id = web_app_version.id
        schedule = SkillSchedule(
            skill_id=service.id,
            name="Active service schedule",
            status="active",
            schedule_type="daily",
            schedule_json={"type": "daily", "time": "08:00", "timezone": "UTC", "input": {}},
            input_json={},
            timezone="UTC",
        )
        db.add(schedule)
        db.flush()
        db.add_all(
            [
                SkillRun(
                    skill_id=function.id,
                    status="running",
                    started_at=now,
                    input_json={},
                ),
                SkillRun(
                    skill_id=service.id,
                    status="running",
                    started_at=now,
                    input_json={},
                    source_schedule_id=schedule.id,
                ),
                WebAppInstance(
                    id="instance-active",
                    skill_id=web_app.id,
                    version_id=web_app_version.id,
                    status="healthy",
                    runner_mode="local_dev",
                    capability_token_hash="active-token",
                ),
                IntegrationAuditRecord(
                    skill_id=function.id,
                    version_id=function_version.id,
                    operation_id="github.repository.get",
                    status="running",
                    started_at=now,
                ),
                McpAuditRecord(
                    function_id="backend.codex.call",
                    category="backend_core",
                    status="running",
                    started_at=now,
                ),
                ScheduleOccurrence(
                    occurrence_key="generated-running",
                    schedule_key="skill:99",
                    definition_fingerprint="generated",
                    scheduled_for_at=now,
                    trigger_reason="automatic",
                    status="running",
                    started_at=now,
                ),
                ScheduleOccurrence(
                    occurrence_key="platform-running",
                    schedule_key="platform:backend.notion.todo.cleanup_done",
                    definition_fingerprint="platform",
                    scheduled_for_at=now,
                    trigger_reason="automatic",
                    status="running",
                    started_at=now,
                ),
            ]
        )
        db.commit()

        state = RuntimeStateService(db)

        assert state.running_skill_ids() == {function.id, web_app.id, service.id}
        assert state.running_function_ids() == {
            "active_function",
            "github.repository.get",
            "backend.codex.call",
        }
        assert state.running_schedule_ids() == {schedule.id, 99}
        assert state.platform_service_is_running("backend.notion.todo.cleanup_done") is True
        assert state.platform_service_is_running("missing") is False
