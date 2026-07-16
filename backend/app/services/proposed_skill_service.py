import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AgentRun,
    ApprovalRequest,
    Skill,
    SkillGenerationRequest,
    SkillOperationLock,
    SkillRun,
    SkillSchedule,
    SkillVersion,
)
from app.schemas.proposed_skill import ProposedSkillValidationRead, SkillFileRead
from app.services.manifest_validator import ManifestValidationError, classify_permission_risk, validate_manifest_file
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard
from app.services.skill_package_files import SkillPackageFileError, read_skill_text, readable_skill_paths

SAFE_SKILL_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
DEFAULT_TIMEOUT_SECONDS = 10
INSTALL_IGNORE_PATTERNS = (
    ".agents",
    ".git",
    ".pytest_cache",
    ".pytest_tmp",
    "__pycache__",
    "*.pyc",
    "codex_last_message.txt",
    "codex_prompt.txt",
    "interface_artifact.json",
)


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

    def create_sample(self, name: str) -> Skill:
        safe_name = self.validate_skill_name(name)
        proposed_dir = self.proposed_dir(safe_name)
        installed_dir = self.installed_dir(safe_name)
        if installed_dir.exists():
            raise ProposedSkillError(f"Installed skill already exists: {safe_name}")
        if proposed_dir.exists():
            raise ProposedSkillError(f"Proposed skill already exists: {safe_name}")

        proposed_dir.mkdir(parents=True)
        self._write_sample_files(proposed_dir, safe_name)

        skill = self.db.scalar(select(Skill).where(Skill.name == safe_name))
        values = {
            "description": "Sample skill created for review.",
            "runtime": "function",
            "status": "proposed",
            "risk_level": "low",
            "manifest_path": self._relative_path(proposed_dir / "manifest.json"),
            "instructions_path": None,
            "input_schema_json": None,
            "output_schema_json": None,
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
            active_dir = skill_dir
            skill = self.db.scalar(select(Skill).where(Skill.name == skill_dir.name))
            if skill is not None and skill.status == "installed" and skill.active_version_id:
                active_version = self.db.get(SkillVersion, skill.active_version_id)
                if active_version is not None and active_version.folder_path:
                    candidate_dir = (self.project_root / active_version.folder_path).resolve()
                    installed_root = (self.project_root / "skills" / "installed").resolve()
                    candidate_manifest = candidate_dir / "manifest.json"
                    if candidate_dir.is_relative_to(installed_root) and candidate_manifest.is_file():
                        active_dir = candidate_dir
                        manifest_path = candidate_manifest
            if not manifest_path.is_file():
                version_dirs = sorted((skill_dir / "versions").glob("v*")) if (skill_dir / "versions").is_dir() else []
                for version_dir in version_dirs:
                    candidate = version_dir / "manifest.json"
                    if candidate.is_file():
                        active_dir = version_dir
                        manifest_path = candidate
                        break
            if not manifest_path.is_file():
                continue
            try:
                manifest = validate_manifest_file(manifest_path)
            except ManifestValidationError:
                continue
            skill = skill or self.db.scalar(select(Skill).where(Skill.name == manifest.name))
            if skill is not None:
                if skill.status == "installed":
                    skill.description = manifest.description
                    skill.runtime = manifest.runtime
                    skill.risk_level = classify_permission_risk(manifest.permissions, manifest.dependencies)
                    skill.manifest_path = self._relative_path(manifest_path)
                    skill.instructions_path = manifest.instructions_path
                    skill.input_schema_json = manifest.input_schema
                    skill.output_schema_json = manifest.output_schema
                    skill.installed_path = self._relative_path(active_dir)
                    changed = True
                continue
            skill = Skill(
                name=manifest.name,
                description=manifest.description,
                runtime=manifest.runtime,
                status="installed",
                risk_level=classify_permission_risk(manifest.permissions, manifest.dependencies),
                manifest_path=self._relative_path(manifest_path),
                instructions_path=manifest.instructions_path,
                input_schema_json=manifest.input_schema,
                output_schema_json=manifest.output_schema,
                installed_path=self._relative_path(active_dir),
                enabled=False,
            )
            self.db.add(skill)
            changed = True
        if changed:
            self.db.commit()

    def read_skill_files(self, skill: Skill) -> list[SkillFileRead]:
        skill_dir = self.skill_dir_for_record(skill)
        return [
            SkillFileRead(path=relative_path, content=read_skill_text(skill_dir, relative_path))
            for relative_path in readable_skill_paths(skill_dir)
        ]

    def read_skill_file(self, skill: Skill, relative_path: str) -> SkillFileRead:
        normalized = relative_path.replace("\\", "/")
        skill_dir = self.skill_dir_for_record(skill)
        try:
            return SkillFileRead(path=normalized, content=read_skill_text(skill_dir, normalized))
        except SkillPackageFileError as exc:
            raise ProposedSkillError(str(exc)) from exc

    def validate_proposed_skill(self, skill: Skill) -> ProposedSkillValidationRead:
        skill_dir = self.skill_dir_for_record(skill)
        try:
            manifest = validate_manifest_file(skill_dir / "manifest.json")
        except (ManifestValidationError, ProposedSkillError, FileNotFoundError) as exc:
            return ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                error_message=str(exc),
            )
        if manifest.name != skill.name:
            return ProposedSkillValidationRead(
                ok=False,
                manifest_valid=False,
                tests_run=False,
                error_message=(
                    f"Manifest name {manifest.name!r} does not match the controlled skill name {skill.name!r}"
                ),
            )
        warnings = []
        if manifest.permissions.network:
            warnings.append(
                "This skill requests network access. Runtime execution requires explicit approval and uses container network access in the current MVP."
            )
        dependency_result = self._ensure_dependencies_installed(skill, skill_dir, manifest)
        if dependency_result is not None:
            if dependency_result.returncode != 0:
                return ProposedSkillValidationRead(
                    ok=False,
                    manifest_valid=True,
                    tests_run=False,
                    stdout=dependency_result.stdout,
                    stderr=dependency_result.stderr,
                    error_message="Skill dependency installation failed",
                    warnings=warnings,
                )
            warnings.append("Installed approved Python dependencies into the skill-local .deps folder.")

        result = self._run_tests(skill_dir)
        return ProposedSkillValidationRead(
            ok=result.returncode == 0,
            manifest_valid=True,
            tests_run=True,
            tests_passed=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            error_message=None if result.returncode == 0 else "Skill tests failed",
            warnings=warnings,
        )

    def install_proposed_skill(self, skill: Skill) -> Skill:
        if skill.status == "installed":
            return skill
        try:
            with SkillOperationGuard(self.db).locked(skill, "install", reason="Installing proposed skill"):
                return self._install_proposed_skill_locked(skill)
        except SkillOperationConflict as exc:
            raise ProposedSkillError(str(exc)) from exc

    def _install_proposed_skill_locked(self, skill: Skill) -> Skill:
        if skill.status != "proposed":
            raise ProposedSkillError("Only proposed skills can be installed")
        validation = self.validate_proposed_skill(skill)
        if not validation.ok:
            raise ProposedSkillError(validation.error_message or "Proposed skill validation failed")

        proposed_dir = self.skill_dir_for_record(skill)
        safe_name = self.validate_skill_name(skill.name)
        installed_dir = self.installed_dir(safe_name)
        version_dir = installed_dir / "versions" / "v1"
        displaced_install: tuple[Path, Path] | None = None
        if installed_dir.exists():
            displaced_install = (installed_dir, self._quarantine_tree(installed_dir, "partial-install"))
        try:
            shutil.copytree(
                proposed_dir,
                version_dir,
                ignore=shutil.ignore_patterns(*INSTALL_IGNORE_PATTERNS),
            )
            manifest = validate_manifest_file(version_dir / "manifest.json")
            if manifest.name != skill.name:
                raise ProposedSkillError(
                    f"Manifest name {manifest.name!r} does not match the controlled skill name {skill.name!r}"
                )
        except Exception as exc:
            self._remove_tree_best_effort(installed_dir)
            if displaced_install is not None:
                self._restore_quarantined_tree(*displaced_install)
            if isinstance(exc, OSError):
                raise ProposedSkillError(f"Could not stage the installed skill package: {exc}") from exc
            raise
        manifest_json = manifest.model_dump(mode="json")
        version = SkillVersion(
            skill_id=skill.id,
            version="v1",
            status="active",
            folder_path=self._relative_path(version_dir),
            code_snapshot_path=self._relative_path(version_dir),
            manifest_json=manifest_json,
            change_summary="Initial installed version.",
            changelog="Initial installed version.",
            created_by="system",
            permission_fingerprint=self._permission_fingerprint(manifest_json),
            test_status="passed",
            validation_status="passed",
        )
        self.db.add(version)
        self.db.flush()
        skill.runtime = manifest.runtime
        skill.status = "installed"
        skill.risk_level = classify_permission_risk(manifest.permissions, manifest.dependencies)
        skill.manifest_path = self._relative_path(version_dir / "manifest.json")
        skill.instructions_path = manifest.instructions_path
        skill.input_schema_json = manifest.input_schema
        skill.output_schema_json = manifest.output_schema
        skill.installed_path = self._relative_path(version_dir)
        skill.active_version_id = version.id
        skill.enabled = False
        try:
            self._register_manifest_schedule(skill)
            self.db.commit()
            self.db.refresh(skill)
        except Exception:
            self.db.rollback()
            self._remove_tree_best_effort(installed_dir)
            if displaced_install is not None:
                self._restore_quarantined_tree(*displaced_install)
            raise
        proposed_trash = self._quarantine_tree(proposed_dir, "installed-source", required=False)
        if proposed_trash is not None:
            self._remove_tree_best_effort(proposed_trash)
        if displaced_install is not None:
            self._remove_tree_best_effort(displaced_install[1])
        return skill

    def reject_proposed_skill(self, skill: Skill) -> None:
        if skill.status != "proposed":
            raise ProposedSkillError("Only proposed skills can be rejected")
        self.delete_skill(skill)

    def delete_skill(self, skill: Skill) -> None:
        guard = SkillOperationGuard(self.db)
        try:
            guard.acquire(skill, "delete", reason="Deleting skill")
        except SkillOperationConflict as exc:
            raise ProposedSkillError(str(exc)) from exc
        staged_trees: list[tuple[Path, Path]] = []
        try:
            for path, label in (
                (self.proposed_dir(skill.name), "deleted-proposed"),
                (self.installed_dir(skill.name), "deleted-installed"),
            ):
                if path.exists():
                    staged_trees.append((path, self._quarantine_tree(path, label)))
            from app.services.web_app_runtime_service import WebAppRuntimeService

            WebAppRuntimeService(self.db, project_root=self.project_root).delete_skill_runtime_records(skill)
            self.db.query(ApprovalRequest).filter(ApprovalRequest.skill_id == skill.id).delete(synchronize_session=False)
            self.db.query(SkillSchedule).filter(SkillSchedule.skill_id == skill.id).delete(synchronize_session=False)
            self.db.query(SkillRun).filter(SkillRun.skill_id == skill.id).delete(synchronize_session=False)
            self.db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).delete(synchronize_session=False)
            self.db.query(SkillOperationLock).filter(SkillOperationLock.skill_id == skill.id).delete(
                synchronize_session=False
            )
            generation_requests = self.db.scalars(
                select(SkillGenerationRequest).where(SkillGenerationRequest.proposed_skill_id == skill.id)
            ).all()
            for generation_request in generation_requests:
                generation_request.proposed_skill_id = None
            for agent_run in self.db.scalars(select(AgentRun).where(AgentRun.skill_id == skill.id)).all():
                agent_run.skill_id = None
            self.db.delete(skill)
            self.db.commit()
        except Exception:
            self.db.rollback()
            for original, quarantined in reversed(staged_trees):
                self._restore_quarantined_tree(original, quarantined)
            guard.release(skill.id)
            raise
        for _original, quarantined in staged_trees:
            self._remove_tree_best_effort(quarantined)

    def validate_skill_name(self, name: str) -> str:
        if not SAFE_SKILL_NAME.fullmatch(name):
            raise ProposedSkillError("Skill name must match ^[a-zA-Z0-9_-]+$")
        return name

    def proposed_dir(self, name: str) -> Path:
        return self._safe_child(self.proposed_root, name)

    def installed_dir(self, name: str) -> Path:
        return self._safe_child(self.installed_root, name)

    def prepare_generation_workspace(self, name: str) -> Path:
        workspace = self.proposed_dir(self.validate_skill_name(name))
        displaced = self._quarantine_tree(workspace, "replaced-generation", required=True)
        try:
            workspace.mkdir(parents=True)
        except OSError as exc:
            if displaced is not None:
                self._restore_quarantined_tree(workspace, displaced)
            raise ProposedSkillError(f"Could not create a clean generation workspace: {workspace}") from exc
        if displaced is not None:
            self._remove_tree_best_effort(displaced)
        return workspace

    def _quarantine_tree(self, path: Path, label: str, *, required: bool = True) -> Path | None:
        if not path.exists():
            return None
        trash_root = (self.project_root / "runtime" / "file_trash").resolve()
        trash_root.mkdir(parents=True, exist_ok=True)
        destination = trash_root / f"{label}-{path.name}-{uuid4().hex}"
        try:
            os.replace(path, destination)
        except OSError as exc:
            if not required:
                return None
            raise ProposedSkillError(
                f"Could not safely stage skill folder for cleanup: {path}. Close processes using it and retry."
            ) from exc
        return destination

    @staticmethod
    def _restore_quarantined_tree(original: Path, quarantined: Path) -> None:
        if not quarantined.exists() or original.exists():
            return
        original.parent.mkdir(parents=True, exist_ok=True)
        os.replace(quarantined, original)

    @staticmethod
    def _remove_tree_best_effort(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except OSError:
            # The folder is already outside active skill roots. Windows may keep
            # sandbox-created pytest directories locked or ACL-restricted; a
            # later cleanup pass can retry without corrupting lifecycle state.
            return

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

    def _permission_fingerprint(self, manifest_json: dict) -> str:
        payload = {
            "permissions": manifest_json.get("permissions", {}),
            "dependencies": manifest_json.get("dependencies", []),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _register_manifest_schedule(self, skill: Skill) -> None:
        from app.services.scheduler_service import SchedulerService

        SchedulerService(self.db, project_root=self.project_root).create_from_manifest_if_present(skill)

    def _write_sample_files(self, skill_dir: Path, name: str) -> None:
        manifest = self._sample_manifest(name)
        (skill_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (skill_dir / "README.md").write_text(self._sample_readme(name), encoding="utf-8")
        (skill_dir / "skill.py").write_text(self._sample_skill_py(), encoding="utf-8")
        tests_dir = skill_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_skill.py").write_text(self._sample_test_py(), encoding="utf-8")

    def _sample_manifest(self, name: str) -> dict:
        return {
            "manifest_version": 1,
            "name": name,
            "description": "Sample skill for the proposed skill workflow.",
            "runtime": "function",
            "entrypoint": "skill.py",
            "instructions_path": None,
            "input_schema": None,
            "output_schema": None,
            "permissions": {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
            "schedule": None,
        }

    def _sample_readme(self, name: str) -> str:
        return (
            f"# {name}\n\n"
            "Sample skill created by the local proposed skill workflow.\n"
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

    def _ensure_dependencies_installed(
        self,
        skill: Skill,
        skill_dir: Path,
        manifest: object,
    ) -> subprocess.CompletedProcess[str] | None:
        dependencies = list(getattr(manifest, "dependencies", []) or [])
        if not dependencies:
            return None
        approved = self._approved_build_dependencies(skill)
        missing_approval = sorted(set(dependencies) - approved)
        if missing_approval:
            return subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr=(
                    "Build-time dependency approval is missing for: "
                    + ", ".join(missing_approval)
                    + ". Approve these dependencies in the generation plan before installing packages."
                ),
            )
        deps_dir = skill_dir / ".deps"
        deps_dir.mkdir(exist_ok=True)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--target",
                str(deps_dir),
                *dependencies,
            ],
            cwd=skill_dir,
            capture_output=True,
            text=True,
            timeout=max(self.timeout_seconds, 60),
            shell=False,
        )

    def _approved_build_dependencies(self, skill: Skill) -> set[str]:
        generation_request = self.db.scalar(
            select(SkillGenerationRequest)
            .where(SkillGenerationRequest.proposed_skill_id == skill.id)
            .order_by(SkillGenerationRequest.created_at.desc(), SkillGenerationRequest.id.desc())
        )
        if generation_request is None:
            return set()
        approval = self.db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.generation_request_id == generation_request.id)
            .where(ApprovalRequest.request_scope == "build_time")
            .where(ApprovalRequest.status == "approved")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        )
        if approval is None:
            return set()
        return set(approval.requested_dependencies_json or [])

    def _test_env(self, skill_dir: Path) -> dict[str, str]:
        env = os.environ.copy()
        deps_dir = skill_dir / ".deps"
        if deps_dir.is_dir():
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = str(deps_dir) if not existing else f"{deps_dir}{os.pathsep}{existing}"
        return env

    def _run_tests(self, skill_dir: Path) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix=f"personal-agent-pytest-{skill_dir.name}-") as basetemp:
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    basetemp,
                    str(skill_dir / "tests"),
                ],
                cwd=skill_dir,
                capture_output=True,
                text=True,
                env=self._test_env(skill_dir),
                timeout=self.timeout_seconds,
                shell=False,
            )
