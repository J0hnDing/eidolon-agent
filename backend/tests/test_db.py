from sqlalchemy import create_engine, inspect, text

import app.db as db_module
from app.db import engine


def test_application_engine_enables_sqlite_foreign_keys() -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_local_schema_migrates_legacy_statuses_and_task_columns(tmp_path, monkeypatch) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE skills (id INTEGER PRIMARY KEY, status VARCHAR(32), enabled BOOLEAN)"))
        connection.execute(text("INSERT INTO skills (id, status, enabled) VALUES (1, 'disabled', 1)"))
        connection.execute(text("CREATE TABLE skill_schedules (id INTEGER PRIMARY KEY, status VARCHAR(32))"))
        connection.execute(text("INSERT INTO skill_schedules (id, status) VALUES (1, 'deleted')"))
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
    assert "current_task_id" in {column["name"] for column in inspector.get_columns("agent_runs")}
    assert "task_node_id" in {column["name"] for column in inspector.get_columns("agent_run_steps")}
    with legacy_engine.connect() as connection:
        assert connection.execute(text("SELECT status, enabled FROM skills WHERE id = 1")).one() == ("installed", 0)
        assert connection.execute(text("SELECT COUNT(*) FROM skill_schedules")).scalar_one() == 0
        assert connection.execute(text("SELECT current_task_id FROM agent_runs WHERE id = 1")).scalar_one() == "legacy_task"
        assert connection.execute(text("SELECT task_node_id FROM agent_run_steps WHERE id = 1")).scalar_one() == "legacy_task"
