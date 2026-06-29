import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApprovalRequest, Skill, SkillGenerationRequest, SkillRun, SkillVersion
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
            "interface_type": "chat",
            "status": "proposed",
            "risk_level": "low",
            "manifest_path": self._relative_path(proposed_dir / "manifest.json"),
            "instructions_path": "SKILL.md" if skill_type in {"instruction", "hybrid"} else None,
            "input_schema_json": None,
            "output_schema_json": None,
            "tool_ui_schema_json": None,
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

    def sync_installed_from_filesystem(self) -> None:
        if not self.installed_root.exists():
            return
        changed = False
        for skill_dir in self.installed_root.iterdir():
            if not skill_dir.is_dir():
                continue
            manifest_path = skill_dir / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = validate_manifest_file(manifest_path)
            except ManifestValidationError:
                continue
            skill = self.db.scalar(select(Skill).where(Skill.name == manifest.name))
            if skill is not None:
                if skill.status == "installed":
                    skill.description = manifest.description
                    skill.skill_type = manifest.skill_type
                    skill.interface_type = manifest.interface_type
                    skill.risk_level = manifest.risk_level
                    skill.manifest_path = self._relative_path(manifest_path)
                    skill.instructions_path = manifest.instructions_path
                    skill.input_schema_json = manifest.input_schema
                    skill.output_schema_json = manifest.output_schema
                    skill.tool_ui_schema_json = manifest.tool_ui_schema
                    skill.installed_path = self._relative_path(skill_dir)
                    skill.enabled = manifest.enabled
                    changed = True
                continue
            skill = Skill(
                name=manifest.name,
                description=manifest.description,
                skill_type=manifest.skill_type,
                interface_type=manifest.interface_type,
                status="installed",
                risk_level=manifest.risk_level,
                manifest_path=self._relative_path(manifest_path),
                instructions_path=manifest.instructions_path,
                input_schema_json=manifest.input_schema,
                output_schema_json=manifest.output_schema,
                tool_ui_schema_json=manifest.tool_ui_schema,
                installed_path=self._relative_path(skill_dir),
                enabled=manifest.enabled,
            )
            self.db.add(skill)
            changed = True
        if changed:
            self.db.commit()

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
        skill.interface_type = manifest.interface_type
        skill.status = "installed"
        skill.risk_level = manifest.risk_level
        skill.manifest_path = self._relative_path(installed_dir / "manifest.json")
        skill.instructions_path = manifest.instructions_path
        skill.input_schema_json = manifest.input_schema
        skill.output_schema_json = manifest.output_schema
        skill.tool_ui_schema_json = manifest.tool_ui_schema
        skill.installed_path = self._relative_path(installed_dir)
        skill.enabled = manifest.skill_type == "instruction"
        self.db.commit()
        self.db.refresh(skill)
        return skill

    def reject_proposed_skill(self, skill: Skill) -> None:
        if skill.status != "proposed":
            raise ProposedSkillError("Only proposed skills can be rejected")
        self.delete_skill(skill)

    def delete_skill(self, skill: Skill) -> None:
        skill_dir = self.skill_dir_for_record(skill)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        self.db.query(ApprovalRequest).filter(ApprovalRequest.skill_id == skill.id).delete(synchronize_session=False)
        self.db.query(SkillRun).filter(SkillRun.skill_id == skill.id).delete(synchronize_session=False)
        self.db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).delete(synchronize_session=False)
        generation_requests = self.db.scalars(
            select(SkillGenerationRequest).where(SkillGenerationRequest.proposed_skill_id == skill.id)
        ).all()
        for generation_request in generation_requests:
            generation_request.proposed_skill_id = None
        self.db.delete(skill)
        self.db.commit()

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
            "interface_type": "chat",
            "entrypoint": "skill.py" if skill_type in {"automation", "hybrid"} else None,
            "instructions_path": "SKILL.md" if skill_type in {"instruction", "hybrid"} else None,
            "input_schema": None,
            "output_schema": None,
            "tool_ui_schema": None,
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
        readme = (
            f"# {name}\n\n"
            f"Sample `{skill_type}` skill created by the local proposed skill workflow.\n"
        )
        if skill_type in {"automation", "hybrid"}:
            readme += (
                "\n## Input\n\n"
                "Send a JSON object. The friendliest input is:\n\n"
                "```json\n"
                "{\n"
                '  "message": "Hello, assistant",\n'
                '  "label": "Manual test"\n'
                "}\n"
                "```\n\n"
                "`message` is echoed back. `label` is optional and is used in the summary.\n\n"
                "## Output\n\n"
                "The skill returns readable JSON with `title`, `summary`, `echoed_message`, "
                "`received_input`, `suggested_next_input`, and `warnings`.\n"
            )
        return readme

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
            "DEFAULT_MESSAGE = 'Hello from the sample echo skill.'\n\n\n"
            "def build_response(payload):\n"
            "    if not isinstance(payload, dict):\n"
            "        return {\n"
            "            'title': 'Sample Echo Skill',\n"
            "            'summary': 'I can echo JSON objects. Please send an object with a message field.',\n"
            "            'echoed_message': '',\n"
            "            'received_input': payload,\n"
            "            'suggested_next_input': {'message': 'Hello, assistant'},\n"
            "            'warnings': ['Input was not a JSON object.'],\n"
            "        }\n\n"
            "    message = payload.get('message') or payload.get('text') or DEFAULT_MESSAGE\n"
            "    label = payload.get('label') or 'Echo response'\n"
            "    return {\n"
            "        'title': 'Sample Echo Skill',\n"
            "        'summary': f'{label}: {message}',\n"
            "        'echoed_message': message,\n"
            "        'received_input': payload,\n"
            "        'suggested_next_input': {'message': 'Try editing this message and running again.'},\n"
            "        'warnings': [],\n"
            "    }\n\n\n"
            "def main():\n"
            "    raw_input = sys.stdin.read()\n"
            "    try:\n"
            "        payload = json.loads(raw_input or '{}')\n"
            "        response = build_response(payload)\n"
            "    except json.JSONDecodeError as exc:\n"
            "        response = {\n"
            "            'title': 'Sample Echo Skill',\n"
            "            'summary': 'The input was not valid JSON.',\n"
            "            'echoed_message': '',\n"
            "            'received_input': raw_input,\n"
            "            'suggested_next_input': {'message': 'Hello, assistant'},\n"
            "            'warnings': [f'Invalid JSON input: {exc.msg}'],\n"
            "        }\n"
            "    print(json.dumps(response))\n\n"
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
            "        input=json.dumps({'message': 'Hello from the UI', 'label': 'Test run'}),\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            "        timeout=5,\n"
            "        shell=False,\n"
            "    )\n"
            "    assert result.returncode == 0\n"
            "    output = json.loads(result.stdout)\n"
            "    assert output['title'] == 'Sample Echo Skill'\n"
            "    assert output['summary'] == 'Test run: Hello from the UI'\n"
            "    assert output['echoed_message'] == 'Hello from the UI'\n"
            "    assert output['received_input'] == {'message': 'Hello from the UI', 'label': 'Test run'}\n"
            "    assert output['warnings'] == []\n\n\n"
            "def test_sample_skill_reports_invalid_json_as_json():\n"
            "    skill_path = Path(__file__).resolve().parents[1] / 'skill.py'\n"
            "    result = subprocess.run(\n"
            "        [sys.executable, str(skill_path)],\n"
            "        input='{not valid json}',\n"
            "        capture_output=True,\n"
            "        text=True,\n"
            "        timeout=5,\n"
            "        shell=False,\n"
            "    )\n"
            "    assert result.returncode == 0\n"
            "    output = json.loads(result.stdout)\n"
            "    assert output['summary'] == 'The input was not valid JSON.'\n"
            "    assert output['warnings']\n"
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
