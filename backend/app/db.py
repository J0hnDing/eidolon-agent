import json
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'personal_agent.db'}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def create_db_and_tables() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_local_schema()


def ensure_local_schema() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "approval_requests" in table_names:
            columns = {column["name"] for column in inspector.get_columns("approval_requests")}
            if "schedule_id" not in columns:
                connection.execute(text("ALTER TABLE approval_requests ADD COLUMN schedule_id INTEGER"))
        if "integration_connections" in table_names:
            columns = {column["name"] for column in inspector.get_columns("integration_connections")}
            if "passphrase_secret_store_id" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN passphrase_secret_store_id VARCHAR(64)")
                )
            if "passphrase_secret_reference" not in columns:
                connection.execute(
                    text("ALTER TABLE integration_connections ADD COLUMN passphrase_secret_reference VARCHAR(256)")
                )
        if "skills" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skills")}
            if "runtime" not in columns:
                connection.execute(
                    text("ALTER TABLE skills ADD COLUMN runtime VARCHAR(32) NOT NULL DEFAULT 'function'")
                )
            if "input_schema_json" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN input_schema_json JSON"))
            if "output_schema_json" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN output_schema_json JSON"))
            if "function_requirements_json" not in columns:
                connection.execute(
                    text("ALTER TABLE skills ADD COLUMN function_requirements_json JSON NOT NULL DEFAULT '[]'")
                )
            if "integration_requirements_json" not in columns:
                connection.execute(
                    text("ALTER TABLE skills ADD COLUMN integration_requirements_json JSON NOT NULL DEFAULT '[]'")
                )
            if "active_version_id" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN active_version_id INTEGER"))
            connection.execute(text("UPDATE skills SET status = 'installed', enabled = 0 WHERE status = 'disabled'"))
            if "skill_type" in columns:
                connection.execute(text("ALTER TABLE skills DROP COLUMN skill_type"))
            if "interface_type" in columns:
                connection.execute(text("ALTER TABLE skills DROP COLUMN interface_type"))
            if "tool_ui_schema_json" in columns:
                connection.execute(text("ALTER TABLE skills DROP COLUMN tool_ui_schema_json"))
        if "skill_generation_requests" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_generation_requests")}
            if "product_manager_thread_id" not in columns:
                connection.execute(
                    text("ALTER TABLE skill_generation_requests ADD COLUMN product_manager_thread_id VARCHAR(128)")
                )
            if "proposed_skill_type" in columns:
                connection.execute(text("ALTER TABLE skill_generation_requests DROP COLUMN proposed_skill_type"))
        if "skill_schedules" in table_names:
            connection.execute(text("DELETE FROM skill_schedules WHERE status = 'deleted'"))
        if "skill_versions" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_versions")}
            if "status" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'draft'"))
            if "folder_path" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN folder_path VARCHAR(512) NOT NULL DEFAULT ''"))
                connection.execute(text("UPDATE skill_versions SET folder_path = code_snapshot_path WHERE folder_path = ''"))
            if "activated_at" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN activated_at DATETIME"))
            if "created_by" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN created_by VARCHAR(32) NOT NULL DEFAULT 'system'"))
            if "parent_version_id" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN parent_version_id INTEGER"))
            if "permission_fingerprint" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN permission_fingerprint VARCHAR(128) NOT NULL DEFAULT ''"))
            if "test_status" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN test_status VARCHAR(32) NOT NULL DEFAULT 'not_run'"))
            if "validation_status" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN validation_status VARCHAR(32) NOT NULL DEFAULT 'not_run'"))
            if "changelog" not in columns:
                connection.execute(text("ALTER TABLE skill_versions ADD COLUMN changelog TEXT"))
        if "skill_runs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_runs")}
            if "codex_invocations_json" not in columns:
                connection.execute(text("ALTER TABLE skill_runs ADD COLUMN codex_invocations_json JSON NOT NULL DEFAULT '[]'"))
            for column in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            ):
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE skill_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"))
            skill_run_columns = {
                "version_id": "INTEGER",
                "invocation_source": "VARCHAR(32) NOT NULL DEFAULT 'internal'",
                "caller_skill_id": "INTEGER",
                "caller_version_id": "INTEGER",
                "source_schedule_id": "INTEGER",
                "web_app_instance_id": "VARCHAR(64)",
                "initiating_action": "VARCHAR(128)",
                "function_capability_token_hash": "VARCHAR(128)",
            }
            for column, definition in skill_run_columns.items():
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE skill_runs ADD COLUMN {column} {definition}"))
            for column in (
                "version_id",
                "invocation_source",
                "caller_skill_id",
                "caller_version_id",
                "source_schedule_id",
                "web_app_instance_id",
                "function_capability_token_hash",
            ):
                connection.execute(
                    text(f"CREATE INDEX IF NOT EXISTS ix_skill_runs_{column} ON skill_runs ({column})")
                )
        if "web_app_instances" in table_names:
            columns = {column["name"] for column in inspector.get_columns("web_app_instances")}
            if "relay_container_id" not in columns:
                connection.execute(text("ALTER TABLE web_app_instances ADD COLUMN relay_container_id VARCHAR(128)"))
        if "agent_runs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("agent_runs")}
            if "current_task_id" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN current_task_id VARCHAR(128)"))
            if "current_milestone" in columns:
                connection.execute(
                    text(
                        "UPDATE agent_runs SET current_task_id = current_milestone "
                        "WHERE current_task_id IS NULL AND current_milestone IS NOT NULL"
                    )
                )
            if "failure_count_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN failure_count_json JSON NOT NULL DEFAULT '{}'"))
            if "build_workflow" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN build_workflow VARCHAR(32)"))
                connection.execute(text("UPDATE agent_runs SET build_workflow = 'task_dag' WHERE run_type = 'build_skill'"))
            if "blueprint_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN blueprint_json JSON"))
            if "final_summary_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN final_summary_json JSON"))
            for column in (
                "total_input_tokens",
                "total_cached_input_tokens",
                "total_output_tokens",
                "total_reasoning_output_tokens",
                "total_tokens",
            ):
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE agent_runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"))
            if "pause_reason" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN pause_reason TEXT"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'product_manager' WHERE current_step IN ('planner', 'reviewer')"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'product_manager' WHERE current_step IN ('permission_analyst', 'security_reviewer')"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'builder' WHERE current_step = 'repairer'"))
        if "agent_run_steps" in table_names:
            columns = {column["name"] for column in inspector.get_columns("agent_run_steps")}
            if "action" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN action VARCHAR(96)"))
            if "task_node_id" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN task_node_id VARCHAR(128)"))
            if "approval_request_id" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN approval_request_id INTEGER"))
            if "agent_input_text" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN agent_input_text TEXT"))
            if "agent_output_text" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN agent_output_text TEXT"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_agent_run_steps_action ON agent_run_steps (action)")
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_run_steps_approval_request_id "
                    "ON agent_run_steps (approval_request_id)"
                )
            )
            if "milestone_name" in columns:
                connection.execute(
                    text(
                        "UPDATE agent_run_steps SET task_node_id = milestone_name "
                        "WHERE task_node_id IS NULL AND milestone_name IS NOT NULL"
                    )
                )
            if "codex_invocations_json" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN codex_invocations_json JSON NOT NULL DEFAULT '[]'"))
            for column in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            ):
                if column not in columns:
                    connection.execute(text(f"ALTER TABLE agent_run_steps ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'product_manager' WHERE step_name IN ('planner', 'reviewer')"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'product_manager' WHERE step_name IN ('permission_analyst', 'security_reviewer')"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'builder' WHERE step_name = 'repairer'"))
        _remove_retired_product_manager_routing(connection, table_names)
        _remove_retired_skill_contract_json(connection, table_names)


def _remove_retired_product_manager_routing(connection, table_names: set[str]) -> None:
    """Drop routing keys that no longer have a live ProductManager action."""

    if "codex_routing_settings" not in table_names:
        return
    rows = connection.execute(
        text("SELECT id, settings_json FROM codex_routing_settings WHERE settings_json IS NOT NULL")
    ).all()
    for row_id, raw_value in rows:
        try:
            value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        product_manager = value.get("product_manager")
        if not isinstance(product_manager, dict) or "plausibility_review" not in product_manager:
            continue
        cleaned = dict(value)
        cleaned_product_manager = dict(product_manager)
        cleaned_product_manager.pop("plausibility_review", None)
        cleaned["product_manager"] = cleaned_product_manager
        connection.execute(
            text("UPDATE codex_routing_settings SET settings_json = :value WHERE id = :row_id"),
            {"value": json.dumps(cleaned), "row_id": row_id},
        )


def _remove_retired_skill_contract_json(connection, table_names: set[str]) -> None:
    json_columns = {
        "skill_generation_requests": ("plan_json",),
        "skill_versions": ("manifest_json",),
        "agent_runs": ("blueprint_json", "final_summary_json"),
        "agent_run_steps": ("input_json", "output_json"),
        "approval_requests": ("reason_json",),
    }
    inspector = inspect(engine)
    for table_name, candidates in json_columns.items():
        if table_name not in table_names:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in candidates:
            if column_name not in columns:
                continue
            rows = connection.execute(
                text(f"SELECT id, {column_name} FROM {table_name} WHERE {column_name} IS NOT NULL")
            ).all()
            for row_id, raw_value in rows:
                try:
                    value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
                except (TypeError, json.JSONDecodeError):
                    continue
                cleaned, changed = _without_retired_skill_fields(value)
                if changed:
                    connection.execute(
                        text(f"UPDATE {table_name} SET {column_name} = :value WHERE id = :row_id"),
                        {"value": json.dumps(cleaned), "row_id": row_id},
                    )


def _without_retired_skill_fields(value):
    if isinstance(value, dict):
        retired_fields = {
            "interface_type",
            "proposed_skill_type",
            "skill_type",
            "tool_ui_schema",
            "tool_ui_schema_json",
        }
        changed = any(key in value for key in retired_fields)
        cleaned = {}
        for key, item in value.items():
            if key in retired_fields:
                continue
            cleaned_item, item_changed = _without_retired_skill_fields(item)
            cleaned[key] = cleaned_item
            changed = changed or item_changed
        return cleaned, changed
    if isinstance(value, list):
        cleaned = []
        changed = False
        for item in value:
            cleaned_item, item_changed = _without_retired_skill_fields(item)
            cleaned.append(cleaned_item)
            changed = changed or item_changed
        return cleaned, changed
    return value, False


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
