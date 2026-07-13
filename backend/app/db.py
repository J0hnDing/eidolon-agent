from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DATA_DIR / 'personal_agent.db'}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
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
            if "current_milestone" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN current_milestone VARCHAR(128)"))
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
            if "milestone_name" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN milestone_name VARCHAR(128)"))
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


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
