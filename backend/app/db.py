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


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
