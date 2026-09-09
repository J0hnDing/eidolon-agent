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
                "CREATE TABLE skill_model_catalog ("
                "skill_id INTEGER PRIMARY KEY, model VARCHAR(128) NOT NULL, updated_at DATETIME NOT NULL)"
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
        connection.execute(
            text(
                "CREATE TABLE skill_versions ("
                "id INTEGER PRIMARY KEY, manifest_json JSON, code_snapshot_path VARCHAR(512))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_versions (id, manifest_json, code_snapshot_path) VALUES "
                "(1, '{\"name\": \"legacy_skill\", \"display_name\": \"Legacy Skill\"}', "
                "'skills/installed/legacy_skill/versions/v1')"
            )
        )
        connection.execute(text("CREATE TABLE skill_schedules (id INTEGER PRIMARY KEY, status VARCHAR(32))"))
        connection.execute(text("INSERT INTO skill_schedules (id, status) VALUES (1, 'deleted')"))
        connection.execute(
            text(
                "CREATE TABLE schedule_runtime_states ("
                "schedule_key VARCHAR(160) PRIMARY KEY, definition_fingerprint VARCHAR(64))"
            )
        )
        connection.execute(text("CREATE TABLE codex_routing_settings (id INTEGER PRIMARY KEY, settings_json JSON)"))
        connection.execute(
            text(
                "INSERT INTO codex_routing_settings (id, settings_json) VALUES "
                "(1, '{\"product_manager\": {\"default\": {}, "
                "\"plausibility_review\": {\"model\": \"legacy\"}, "
                "\"blueprint_and_permissions\": {\"model\": \"current\"}}}')"
            )
        )
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
        connection.execute(text("CREATE TABLE act_turns (id INTEGER PRIMARY KEY)"))
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
    assert "function_requirements_json" in skill_columns
    assert "reasoning_effort" in {
        column["name"] for column in inspector.get_columns("skill_model_catalog")
    }
    assert "availability_migrated" in {
        column["name"] for column in inspector.get_columns("skill_schedules")
    }
    assert "relay_container_id" in {
        column["name"] for column in inspector.get_columns("web_app_instances")
    }
    assert {"enabled", "configuration_json"} <= {
        column["name"]
        for column in inspector.get_columns("schedule_runtime_states")
    }
    assert "proposed_skill_type" not in {
        column["name"] for column in inspector.get_columns("skill_generation_requests")
    }
    assert "current_task_id" in {column["name"] for column in inspector.get_columns("agent_runs")}
    agent_step_columns = {column["name"] for column in inspector.get_columns("agent_run_steps")}
    assert {
        "action",
        "task_node_id",
        "approval_request_id",
        "agent_input_text",
        "agent_output_text",
    } <= agent_step_columns
    assert "started_at" in {column["name"] for column in inspector.get_columns("act_turns")}
    with legacy_engine.connect() as connection:
        assert connection.execute(text("SELECT status, enabled, runtime FROM skills WHERE id = 1")).one() == (
            "installed",
            0,
            "function",
        )
        assert connection.execute(text("SELECT COUNT(*) FROM skill_schedules")).scalar_one() == 0
        routing_json = connection.execute(
            text("SELECT settings_json FROM codex_routing_settings WHERE id = 1")
        ).scalar_one()
        assert json.loads(routing_json) == {
            "product_manager": {
                "default": {},
                "blueprint_and_permissions": {"model": "current"},
            }
        }
        assert connection.execute(text("SELECT current_task_id FROM agent_runs WHERE id = 1")).scalar_one() == "legacy_task"
        assert connection.execute(text("SELECT task_node_id FROM agent_run_steps WHERE id = 1")).scalar_one() == "legacy_task"
        plan_json = connection.execute(
            text("SELECT plan_json FROM skill_generation_requests WHERE id = 1")
        ).scalar_one()
        assert json.loads(plan_json) == {"nested": {}}
        manifest_json = connection.execute(
            text("SELECT manifest_json FROM skill_versions WHERE id = 1")
        ).scalar_one()
        assert json.loads(manifest_json) == {"name": "legacy_skill"}


def test_local_schema_migrates_channel_session_bindings(tmp_path, monkeypatch) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'channel-legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE act_sessions ("
                "id INTEGER PRIMARY KEY, agent_id VARCHAR(32), proposal_count INTEGER)"
            )
        )
        connection.execute(text("INSERT INTO act_sessions (id, agent_id, proposal_count) VALUES (7, 'observer', 0)"))
        connection.execute(
            text(
                "CREATE TABLE telegram_bot_connections ("
                "id INTEGER PRIMARY KEY, role VARCHAR(64))"
            )
        )
        connection.execute(text("INSERT INTO telegram_bot_connections (id, role) VALUES (3, 'observer_agent')"))
        connection.execute(
            text(
                "CREATE TABLE act_telegram_bindings ("
                "connection_id INTEGER PRIMARY KEY, active_session_id INTEGER)"
            )
        )
        connection.execute(text("INSERT INTO act_telegram_bindings VALUES (3, 7)"))
        connection.execute(
            text(
                "CREATE TABLE wecom_observer_user_bindings ("
                "id INTEGER PRIMARY KEY, connection_id INTEGER, paired_user_id VARCHAR(128), "
                "active_session_id INTEGER, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE wecom_observer_bindings ("
                "connection_id INTEGER PRIMARY KEY, paired_user_id VARCHAR(128), "
                "active_session_id INTEGER, pairing_code_hash VARCHAR(128), "
                "pairing_expires_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO wecom_observer_bindings "
                "(connection_id, paired_user_id, active_session_id, updated_at) "
                "VALUES (9, 'legacy-user', 7, CURRENT_TIMESTAMP)"
            )
        )

    monkeypatch.setattr(db_module, "engine", legacy_engine)
    db_module.ensure_local_schema()

    inspector = inspect(legacy_engine)
    assert "act_telegram_bindings" not in inspector.get_table_names()
    assert {"topics_enabled", "allows_users_to_create_topics"} <= {
        column["name"] for column in inspector.get_columns("telegram_bot_connections")
    }
    user_columns = {column["name"] for column in inspector.get_columns("wecom_observer_user_bindings")}
    assert "current_session_id" in user_columns
    with legacy_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT connection_id, paired_user_id, current_session_id "
                "FROM wecom_observer_user_bindings"
            )
        ).all() == [(9, "legacy-user", 7)]
        assert connection.execute(
            text(
                "SELECT paired_user_id, active_session_id "
                "FROM wecom_observer_bindings WHERE connection_id = 9"
            )
        ).one() == (None, None)
