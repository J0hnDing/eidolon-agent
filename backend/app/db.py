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
        if "skills" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skills")}
            if "interface_type" not in columns:
                connection.execute(
                    text("ALTER TABLE skills ADD COLUMN interface_type VARCHAR(16) NOT NULL DEFAULT 'chat'")
                )
            if "input_schema_json" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN input_schema_json JSON"))
            if "output_schema_json" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN output_schema_json JSON"))
            if "tool_ui_schema_json" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN tool_ui_schema_json JSON"))
            if "active_version_id" not in columns:
                connection.execute(text("ALTER TABLE skills ADD COLUMN active_version_id INTEGER"))
            connection.execute(text("UPDATE skills SET status = 'installed', enabled = 0 WHERE status = 'disabled'"))
            if "skill_type" in columns:
                connection.execute(text("ALTER TABLE skills DROP COLUMN skill_type"))
        if "skill_generation_requests" in table_names:
            columns = {column["name"] for column in inspector.get_columns("skill_generation_requests")}
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
            if "task_node_id" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN task_node_id VARCHAR(128)"))
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
        _remove_legacy_skill_type_json(connection, table_names)


def _remove_legacy_skill_type_json(connection, table_names: set[str]) -> None:
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
                cleaned, changed = _without_skill_type(value)
                if changed:
                    connection.execute(
                        text(f"UPDATE {table_name} SET {column_name} = :value WHERE id = :row_id"),
                        {"value": json.dumps(cleaned), "row_id": row_id},
                    )


def _without_skill_type(value):
    if isinstance(value, dict):
        changed = "skill_type" in value or "proposed_skill_type" in value
        cleaned = {}
        for key, item in value.items():
            if key in {"skill_type", "proposed_skill_type"}:
                continue
            cleaned_item, item_changed = _without_skill_type(item)
            cleaned[key] = cleaned_item
            changed = changed or item_changed
        return cleaned, changed
    if isinstance(value, list):
        cleaned = []
        changed = False
        for item in value:
            cleaned_item, item_changed = _without_skill_type(item)
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
