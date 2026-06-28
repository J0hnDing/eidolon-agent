from app.db import SessionLocal, create_db_and_tables
from app.services.demo_skill_seed import register_personal_news_digest


def main() -> None:
    create_db_and_tables()
    db = SessionLocal()
    try:
        skill = register_personal_news_digest(db)
        print(f"Registered {skill.name} with id {skill.id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
