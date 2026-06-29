import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Skill
from app.schemas.common import SkillType
from app.schemas.proposed_skill import ProposedSkillValidationRead, SkillFileRead
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file


SAFE_SKILL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
READABLE_FILES = ("manifest.json", "README.md", "SKILL.md", "skill.py", "tests/test_skill.py")
DEFAULT_TIMEOUT_SECONDS = 10


class ProposedSkillError(ValueError):
    pass


@dataclass
class ProposedSkillService:
    db: Session
    project_root: Path | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.project_root is None:
            self.project_root = Path(__file__).resolve().parents[3]
        self.project_root = self.project_root.resolve()
        self.proposed_root = self.project_root / "skills" / "proposed"
        self.installed_root = self.project_root / "skills" / "installed"

    def create_sample(self, name: str, skill_type: SkillType) -> Skill:
        safe_name = self.validate_skill_name(name)
        proposed_dir = self.proposed_dir(safe_name)
        installed_dir = self.installed_dir(safe_name)
        if installed_dir.exists():
            raise ProposedSkillError(f"Installed skill already exists: {safe_name}")
        if proposed_dir.exists():
            raise ProposedSkillError(f"Proposed skill already exists: {safe_name}")

        proposed_dir.mkdir(parents=True)
        self._write_sample_files(proposed_dir, safe_name, skill_type)

        skill = self.db.scalar(select(Skill).where(Skill.name == safe_name))
        values = {
            "description": f"Sample {skill_type} skill created for review.",
            "skill_type": skill_type,
            "status": "proposed",
            "risk_level": "low",
            "manifest_path": self._relative_path(proposed_dir / "manifest.json"),
            "instructions_path": "SKILL.md" if skill_type in {"instruction", "hybrid"} else None,
            "installed_path": None,
            "enabled": False,
        }
        if skill is None:
            skill = Skill(name=safe_name, **values)
            self.db.add(skill)
        else:
            for key, value in values.items():
                setattr(skill, key, value)

        self.db.commit()
        self.db.refresh(skill)
        return skill

    def list_proposed(self) -> list[Skill]:
        return list(
            self.db.scalars(
                select(Skill).where(Skill.status == "proposed").order_by(Skill.created_at.desc())
            ).all()
        )

    def read_skill_files(self, skill: Skill) -> list[SkillFileRead]:
        skill_dir = self.skill_dir_for_record(skill)
        files = []
        for relative_path in READABLE_FILES:
            path = skill_dir / relative_path
            if path.is_file():
                files.append(SkillFileRead(path=relative_path, content=path.read_text(encoding="utf-8")))
        return files

    def read_skill_file(self, skill: Skill, relative_path: str) -> SkillFileRead:
        normalized = relative_path.replace("\\", "/")
        if normalized not in READABLE_FILES:
            raise ProposedSkillError("Requested file is not readable through this workflow")
        skill_dir = self.skill_dir_for_record(skill)
        path = (skill_dir / normalized).resolve()
        if not path.is_relative_to(skill_dir.resolve()):
            raise ProposedSkillError("Requested file must stay inside the skill folder")
        if not path.is_file():
            raise ProposedSkillError("Requested file does not exist")
        return SkillFileRead(path=normalized, content=path.read_text(encoding="utf-8"))

    def validate_proposed_skill(self, skill: Skill) -> ProposedSkillValidationRead:
        skill_dir = self.skill_dir_for_record(skill)
        try:
            manifest = validate_manifest_file(skill_dir / "manifest.json")
            self._validate_declared_files(skill_dir, manifest.skill_type, manifest.instructions_path)
        except (ManifestValidationError, ProposedSkillError, FileNotFoundError) as exc:
            return ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                error_message=str(exc),
            )
        warnings = []
        if manifest.permissions.network:
            warnings.append(
                "This skill requests network access, but the current runner does not support networked execution yet."
            )

        if manifest.skill_type == "instruction":
            tests_result = self._run_optional_instruction_tests(skill_dir)
            if tests_result is not None:
                tests_result.warnings = warnings
                return tests_result
            return ProposedSkillValidationRead(
                ok=True,
                skill_type=manifest.skill_type,
                manifest_valid=True,
                tests_run=False,
                warnings=warnings,
            )

        result = self._run_tests(skill_dir)
        return ProposedSkillValidationRead(
            ok=result.returncode == 0,
            skill_type=manifest.skill_type,
            manifest_valid=True,
            tests_run=True,
            tests_passed=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            error_message=None if result.returncode == 0 else "Skill tests failed",
            warnings=warnings,
        )

    def install_proposed_skill(self, skill: Skill) -> Skill:
        if skill.status != "proposed":
            raise ProposedSkillError("Only proposed skills can be installed")

        validation = self.validate_proposed_skill(skill)
        if not validation.ok:
            raise ProposedSkillError(validation.error_message or "Proposed skill validation failed")

        proposed_dir = self.skill_dir_for_record(skill)
        safe_name = self.validate_skill_name(skill.name)
        installed_dir = self.installed_dir(safe_name)
        if installed_dir.exists():
            raise ProposedSkillError(f"Installed skill already exists: {safe_name}")

        shutil.copytree(
            proposed_dir,
            installed_dir,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        shutil.rmtree(proposed_dir)

        manifest = validate_manifest_file(installed_dir / "manifest.json")
        skill.skill_type = manifest.skill_type
        skill.status = "installed"
        skill.risk_level = manifest.risk_level
        skill.manifest_path = self._relative_path(installed_dir / "manifest.json")
        skill.instructions_path = manifest.instructions_path
        skill.installed_path = self._relative_path(installed_dir)
        skill.enabled = manifest.skill_type == "instruction"
        self.db.commit()
        self.db.refresh(skill)
        return skill

    def reject_proposed_skill(self, skill: Skill) -> Skill:
        if skill.status == "proposed":
            skill_dir = self.skill_dir_for_record(skill)
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
        skill.status = "deleted"
        skill.enabled = False
        self.db.commit()
        self.db.refresh(skill)
        return skill

    def validate_skill_name(self, name: str) -> str:
        if not SAFE_SKILL_NAME.fullmatch(name):
            raise ProposedSkillError("Skill name must match ^[a-zA-Z0-9_-]+$")
        return name

    def proposed_dir(self, name: str) -> Path:
        return self._safe_child(self.proposed_root, name)

    def installed_dir(self, name: str) -> Path:
        return self._safe_child(self.installed_root, name)

    def skill_dir_for_record(self, skill: Skill) -> Path:
        raw_path = skill.installed_path or skill.manifest_path
        path = Path(raw_path)
        if path.is_absolute():
            raise ProposedSkillError("Skill paths must be relative to the project root")
        resolved = (self.project_root / path).resolve()
        if resolved.name == "manifest.json":
            resolved = resolved.parent
        allowed_roots = [self.proposed_root.resolve(), self.installed_root.resolve()]
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            raise ProposedSkillError("Skill path must stay inside skills/proposed or skills/installed")
        return resolved

    def _safe_child(self, root: Path, name: str) -> Path:
        root = root.resolve()
        child = (root / name).resolve()
        if not child.is_relative_to(root):
            raise ProposedSkillError("Skill path must stay inside the expected skill root")
        return child

    def _relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.project_root).as_posix()

    def _write_sample_files(self, skill_dir: Path, name: str, skill_type: SkillType) -> None:
        manifest = self._sample_manifest(name, skill_type)
        (skill_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (skill_dir / "README.md").write_text(self._sample_readme(name, skill_type), encoding="utf-8")
        if skill_type in {"instruction", "hybrid"}:
            (skill_dir / "SKILL.md").write_text(self._sample_instructions(name), encoding="utf-8")
        if skill_type in {"automation", "hybrid"}:
            (skill_dir / "skill.py").write_text(self._sample_skill_py(), encoding="utf-8")
            tests_dir = skill_dir / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_skill.py").write_text(self._sample_test_py(), encoding="utf-8")

    def _sample_manifest(self, name: str, skill_type: SkillType) -> dict:
        return {
            "name": name,
            "description": f"Sample {skill_type} skill for the proposed skill workflow.",
            "skill_type": skill_type,
            "entrypoint": "skill.py" if skill_type in {"automation", "hybrid"} else None,
            "instructions_path": "SKILL.md" if skill_type in {"instruction", "hybrid"} else None,
            "risk_level": "low",
            "permissions": {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": ["./cache"] if skill_type in {"automation", "hybrid"} else [],
                "secrets": [],
                "shell": False,
            },
            "schedule": None,
            "created_by": "manual_sample",
            "enabled": False,
        }

    def _sample_readme(self, name: str, skill_type: SkillType) -> str:
        return (
            f"# {name}\n\n"
            f"Sample `{skill_type}` skill created by the local proposed skill workflow.\n"
        )

    def _sample_instructions(self, name: str) -> str:
        return (
            f"# {name} Instructions\n\n"
            "Use this capability to answer with a short, structured response. "
            "Do not access files, network, secrets, or shell commands.\n"
        )

    def _sample_skill_py(self) -> str:
        return (
            "import json\n"
            "import sys\n\n"
            "def main():\n"
            "    payload = json.loads(sys.stdin.read() or '{}')\n"
            "    print(json.dumps({'title': 'Sample Echo Skill', 'input': payload, 'warnings': []}))\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )

    def _sample_test_py(self) -> str:
        return (
            "import json\n"
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n\n"
            "def test_sample_skill_outputs_json():\n"
            "    skill_path = Path(__file__).resolve().parents[1] / 'skill.py'\n"
            "    result = subprocess.run(\n"
            "        [sys.executable, str(skill_path)],\n"
            "        input=json.dumps({'hello': 'world'}),\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            "        timeout=5,\n"
            "        shell=False,\n"
            "    )\n"
            "    assert result.returncode == 0\n"
            "    assert json.loads(result.stdout)['input'] == {'hello': 'world'}\n"
        )

    def _validate_declared_files(
        self,
        skill_dir: Path,
        skill_type: SkillType,
        instructions_path: str | None,
    ) -> None:
        if skill_type in {"instruction", "hybrid"}:
            if instructions_path is None:
                raise ProposedSkillError(f"{skill_type} skills require instructions_path")
            self._resolve_declared_file(skill_dir, instructions_path)

    def _resolve_declared_file(self, skill_dir: Path, relative_path: str) -> Path:
        path = (skill_dir / relative_path).resolve()
        if not path.is_relative_to(skill_dir.resolve()):
            raise ProposedSkillError("Declared skill files must stay inside the skill folder")
        if not path.is_file():
            raise ProposedSkillError(f"Declared file is missing: {relative_path}")
        return path

    def _run_optional_instruction_tests(
        self,
        skill_dir: Path,
    ) -> ProposedSkillValidationRead | None:
        if not (skill_dir / "tests").is_dir():
            return None
        result = self._run_tests(skill_dir)
        return ProposedSkillValidationRead(
            ok=result.returncode == 0,
            skill_type="instruction",
            manifest_valid=True,
            tests_run=True,
            tests_passed=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            error_message=None if result.returncode == 0 else "Instruction skill tests failed",
        )

    def _run_tests(self, skill_dir: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(skill_dir / "tests")],
            cwd=skill_dir,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )
