import json

from sqlalchemy import create_engine, inspect, text

import app.db as db_module
from app.db import engine


def test_application_engine_enables_sqlite_foreign_keys() -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_local_schema_migrates_legacy_statuses_task_columns_and_retired_skill_fields(
    tmp_path,
    monkeypatch,
) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE skills ("
                "id INTEGER PRIMARY KEY, status VARCHAR(32), enabled BOOLEAN, skill_type VARCHAR(16), "
                "interface_type VARCHAR(16), tool_ui_schema_json JSON)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skills "
                "(id, status, enabled, skill_type, interface_type, tool_ui_schema_json) "
                "VALUES (1, 'disabled', 1, 'automation', 'tool', '{\"title\": \"Legacy\"}')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE skill_generation_requests ("
                "id INTEGER PRIMARY KEY, proposed_skill_type VARCHAR(16), plan_json JSON)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_generation_requests (id, proposed_skill_type, plan_json) "
                "VALUES (1, 'automation', "
                "'{\"skill_type\": \"automation\", \"interface_type\": \"tool\", "
                "\"nested\": {\"skill_type\": \"automation\", \"tool_ui_schema\": {\"fields\": []}}}')"
            )
        )
        connection.execute(text("CREATE TABLE skill_schedules (id INTEGER PRIMARY KEY, status VARCHAR(32))"))
        connection.execute(text("INSERT INTO skill_schedules (id, status) VALUES (1, 'deleted')"))
        connection.execute(text("CREATE TABLE web_app_instances (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE agent_runs ("
                "id INTEGER PRIMARY KEY, current_milestone VARCHAR(128), current_step VARCHAR(64), run_type VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_runs (id, current_milestone, current_step, run_type) "
                "VALUES (1, 'legacy_task', 'builder', 'build_skill')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agent_run_steps ("
                "id INTEGER PRIMARY KEY, milestone_name VARCHAR(128), step_name VARCHAR(64))"
            )
        )
        connection.execute(
            text("INSERT INTO agent_run_steps (id, milestone_name, step_name) VALUES (1, 'legacy_task', 'builder')")
        )

    monkeypatch.setattr(db_module, "engine", legacy_engine)
    db_module.ensure_local_schema()

    inspector = inspect(legacy_engine)
    skill_columns = {column["name"] for column in inspector.get_columns("skills")}
    assert "skill_type" not in skill_columns
    assert "interface_type" not in skill_columns
    assert "tool_ui_schema_json" not in skill_columns
    assert "runtime" in skill_columns
    assert "relay_container_id" in {
        column["name"] for column in inspector.get_columns("web_app_instances")
    }
    assert "proposed_skill_type" not in {
        column["name"] for column in inspector.get_columns("skill_generation_requests")
    }
    assert "current_task_id" in {column["name"] for column in inspector.get_columns("agent_runs")}
    assert "task_node_id" in {column["name"] for column in inspector.get_columns("agent_run_steps")}
    with legacy_engine.connect() as connection:
        assert connection.execute(text("SELECT status, enabled, runtime FROM skills WHERE id = 1")).one() == (
            "installed",
            0,
            "function",
        )
        assert connection.execute(text("SELECT COUNT(*) FROM skill_schedules")).scalar_one() == 0
        assert connection.execute(text("SELECT current_task_id FROM agent_runs WHERE id = 1")).scalar_one() == "legacy_task"
        assert connection.execute(text("SELECT task_node_id FROM agent_run_steps WHERE id = 1")).scalar_one() == "legacy_task"
        plan_json = connection.execute(
            text("SELECT plan_json FROM skill_generation_requests WHERE id = 1")
        ).scalar_one()
        assert json.loads(plan_json) == {"nested": {}}
