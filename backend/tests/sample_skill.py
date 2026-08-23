import json

from sqlalchemy import select

from app.models import Skill
from app.services.proposed_skill_service import ProposedSkillError, ProposedSkillService


def create_sample_skill(service: ProposedSkillService, name: str) -> Skill:
    """Create the minimal proposed package used by service-level tests."""

    safe_name = service.validate_skill_name(name)
    proposed_dir = service.proposed_dir(safe_name)
    if service.installed_dir(safe_name).exists():
        raise ProposedSkillError(f"Installed skill already exists: {safe_name}")
    if proposed_dir.exists():
        raise ProposedSkillError(f"Proposed skill already exists: {safe_name}")

    proposed_dir.mkdir(parents=True)
    manifest = {
        "manifest_version": 1,
        "name": safe_name,
        "description": "Sample skill created for service tests.",
        "runtime": "function",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": None,
        "output_schema": None,
        "function_requirements": [],
        "integration_requirements": [],
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
    }
    (proposed_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (proposed_dir / "README.md").write_text(f"# {safe_name}\n", encoding="utf-8")
    (proposed_dir / "skill.py").write_text("import json\nprint(json.dumps({'ok': True}))\n", encoding="utf-8")
    tests_dir = proposed_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_skill.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

    skill = service.db.scalar(select(Skill).where(Skill.name == safe_name))
    values = {
        "description": manifest["description"],
        "runtime": "function",
        "status": "proposed",
        "risk_level": "low",
        "manifest_path": service._relative_path(proposed_dir / "manifest.json"),
        "instructions_path": None,
        "input_schema_json": None,
        "output_schema_json": None,
        "function_requirements_json": [],
        "integration_requirements_json": [],
        "installed_path": None,
        "enabled": False,
    }
    if skill is None:
        skill = Skill(name=safe_name, **values)
        service.db.add(skill)
    else:
        for key, value in values.items():
            setattr(skill, key, value)
    service.db.commit()
    service.db.refresh(skill)
    return skill
