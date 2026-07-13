from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Skill

DEMO_SKILL_NAME = "personal_news_digest"
DEMO_SKILL_DESCRIPTION = "Offline demo digest that ranks local sample articles by user topics."
DEMO_SKILL_DIR = Path("skills/installed/personal_news_digest")
DEMO_MANIFEST_PATH = DEMO_SKILL_DIR / "manifest.json"


def register_personal_news_digest(db: Session) -> Skill:
    skill = db.scalar(select(Skill).where(Skill.name == DEMO_SKILL_NAME))
    values = {
        "description": DEMO_SKILL_DESCRIPTION,
        "skill_type": "automation",
        "status": "installed",
        "risk_level": "low",
        "manifest_path": DEMO_MANIFEST_PATH.as_posix(),
        "instructions_path": None,
        "installed_path": DEMO_SKILL_DIR.as_posix(),
        "enabled": True,
    }

    if skill is None:
        skill = Skill(name=DEMO_SKILL_NAME, **values)
        db.add(skill)
    else:
        for key, value in values.items():
            setattr(skill, key, value)

    db.commit()
    db.refresh(skill)
    return skill
