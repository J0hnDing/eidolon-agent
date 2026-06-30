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
        if "agent_runs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("agent_runs")}
            if "current_milestone" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN current_milestone VARCHAR(128)"))
            if "failure_count_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN failure_count_json JSON NOT NULL DEFAULT '{}'"))
            if "blueprint_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN blueprint_json JSON"))
            if "final_summary_json" not in columns:
                connection.execute(text("ALTER TABLE agent_runs ADD COLUMN final_summary_json JSON"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'product_manager' WHERE current_step IN ('planner', 'reviewer')"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'security_reviewer' WHERE current_step = 'permission_analyst'"))
            connection.execute(text("UPDATE agent_runs SET current_step = 'builder' WHERE current_step = 'repairer'"))
        if "agent_run_steps" in table_names:
            columns = {column["name"] for column in inspector.get_columns("agent_run_steps")}
            if "milestone_name" not in columns:
                connection.execute(text("ALTER TABLE agent_run_steps ADD COLUMN milestone_name VARCHAR(128)"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'product_manager' WHERE step_name IN ('planner', 'reviewer')"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'security_reviewer' WHERE step_name = 'permission_analyst'"))
            connection.execute(text("UPDATE agent_run_steps SET step_name = 'builder' WHERE step_name = 'repairer'"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
