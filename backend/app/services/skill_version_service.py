import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApprovalRequest, Skill, SkillVersion
from app.schemas.proposed_skill import ProposedSkillValidationRead
from app.services.manifest_validator import validate_manifest_file
from app.services.permission_service import PermissionService
from app.services.proposed_skill_service import ProposedSkillService
from app.services.skill_operation_guard import SkillOperationConflict, SkillOperationGuard

MAX_SKILL_VERSIONS = 3
READABLE_COMPARE_FILES = ("manifest.json", "README.md", "SKILL.md", "skill.py", "tests/test_skill.py")


class SkillVersionError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SkillVersionService:
    db: Session
    project_root: Path | None = None
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if self.project_root is None:
            self.project_root = Path(__file__).resolve().parents[3]
        self.project_root = self.project_root.resolve()
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)

    def list_versions(self, skill: Skill) -> list[SkillVersion]:
        self.ensure_active_version(skill)
        return list(
            self.db.scalars(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill.id)
                .where(SkillVersion.status != "discarded")
                .order_by(SkillVersion.created_at.desc(), SkillVersion.id.desc())
            ).all()
        )

    def ensure_active_version(self, skill: Skill) -> SkillVersion:
        if skill.active_version_id is not None:
            version = self.db.get(SkillVersion, skill.active_version_id)
            if version is not None:
                return version

        existing_active = self.db.scalar(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill.id)
            .where(SkillVersion.status == "active")
            .order_by(SkillVersion.created_at.desc(), SkillVersion.id.desc())
        )
        if existing_active is not None:
            self._point_skill_at_version(skill, existing_active)
            return existing_active

        if skill.status != "installed" or not skill.installed_path:
            raise SkillVersionError("Only installed skills can initialize an active version")

        source_dir = self.proposed_service.skill_dir_for_record(skill)
        version = self._create_initial_version_from_folder(skill, source_dir)
        self._point_skill_at_version(skill, version)
        return version

    def create_draft_from_active(self, skill: Skill, changelog: str, *, created_by: str = "agent") -> SkillVersion:
        active = self.ensure_active_version(skill)
        existing_versions = self._non_discarded_versions(skill)
        if len(existing_versions) >= MAX_SKILL_VERSIONS:
            raise SkillVersionError("Maximum 3 non-discarded versions reached. Delete an inactive version before creating another.")

        next_number = self._next_version_number(existing_versions)
        version_name = f"v{next_number}"
        source_dir = self._version_dir(active)
        destination = self._versions_root(skill) / version_name
        if destination.exists():
            raise SkillVersionError(f"Version folder already exists: {version_name}")
        self._copy_skill_folder(source_dir, destination)
        manifest = validate_manifest_file(destination / "manifest.json")
        version = SkillVersion(
            skill_id=skill.id,
            version=version_name,
            status="draft",
            folder_path=self._relative_path(destination),
            code_snapshot_path=self._relative_path(destination),
            manifest_json=manifest.model_dump(mode="json"),
            change_summary=changelog,
            changelog=changelog,
            created_by=created_by,
            parent_version_id=active.id,
            permission_fingerprint=self.permission_fingerprint(manifest.model_dump(mode="json")),
            test_status="not_run",
            validation_status="not_run",
        )
        self.db.add(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def validate_version(self, version: SkillVersion) -> ProposedSkillValidationRead:
        skill_dir = self._version_dir(version)
        try:
            manifest = validate_manifest_file(skill_dir / "manifest.json")
        except Exception as exc:
            version.validation_status = "failed"
            version.test_status = "not_run"
            self.db.commit()
            return ProposedSkillValidationRead(ok=False, manifest_valid=False, error_message=str(exc))

        version.manifest_json = manifest.model_dump(mode="json")
        version.permission_fingerprint = self.permission_fingerprint(version.manifest_json)
        version.validation_status = "passed"
        if manifest.skill_type == "instruction":
            version.test_status = "not_required"
            version.status = "proposed_update"
            self.db.commit()
            self.db.refresh(version)
            return ProposedSkillValidationRead(ok=True, skill_type="instruction", manifest_valid=True, tests_run=False)

        tests_dir = skill_dir / "tests"
        if not tests_dir.is_dir():
            version.test_status = "failed"
            self.db.commit()
            return ProposedSkillValidationRead(
                ok=False,
                skill_type=manifest.skill_type,
                manifest_valid=True,
                tests_run=False,
                error_message="Skill tests directory is missing",
            )

        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_dir)],
            cwd=skill_dir,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )
        passed = result.returncode == 0
        version.test_status = "passed" if passed else "failed"
        version.status = "proposed_update" if passed else "draft"
        self.db.commit()
        self.db.refresh(version)
        return ProposedSkillValidationRead(
            ok=passed,
            skill_type=manifest.skill_type,
            manifest_valid=True,
            tests_run=True,
            tests_passed=passed,
            stdout=result.stdout,
            stderr=result.stderr,
            error_message=None if passed else "Skill version tests failed",
        )

    def create_runtime_request_if_needed(self, skill: Skill, version: SkillVersion) -> ApprovalRequest | None:
        active = self.ensure_active_version(skill)
        if active.permission_fingerprint == version.permission_fingerprint:
            return None
        existing = self._latest_version_permission_request(skill, version)
        if existing and existing.status in {"pending", "approved", "denied"}:
            return existing

        manifest = version.manifest_json
        permissions = dict(manifest.get("permissions", {}))
        dependencies = list(manifest.get("dependencies", []) or [])
        permission_service = PermissionService(self.db, project_root=self.project_root)
        risk_level, blocked_reasons = permission_service._risk_for_permissions(permissions, dependencies=dependencies)
        explanation = (
            f"Version {version.version} changes runtime permissions for {skill.name}. "
            "Approve this before activating the new version. Activation will not run the skill automatically."
        )
        request = ApprovalRequest(
            skill_id=skill.id,
            request_scope="runtime",
            request_type="install",
            risk_level=risk_level,
            requested_permissions_json=permissions,
            requested_dependencies_json=dependencies,
            requested_network_domains_json=list(permissions.get("network", [])),
            requested_filesystem_json={
                "filesystem_read": permissions.get("filesystem_read", []),
                "filesystem_write": permissions.get("filesystem_write", []),
            },
            reason_json={
                "version_id": version.id,
                "active_version_id": active.id,
                "permission_fingerprint_changed": True,
                "blocked_reasons": blocked_reasons,
                "runner_unsupported": permission_service.unsupported_runtime_reasons(permissions),
            },
            reason=explanation,
            user_explanation=explanation,
            status="pending",
        )
        self.db.add(request)
        self.db.commit()
        self.db.refresh(request)
        return request

    def activate_version(self, skill: Skill, version: SkillVersion) -> Skill:
        if version.skill_id != skill.id:
            raise SkillVersionError("Version does not belong to this skill")
        if version.status not in {"proposed_update", "archived"}:
            raise SkillVersionError("Only proposed update or archived versions can be activated")
        if version.validation_status != "passed" or version.test_status not in {"passed", "not_required"}:
            raise SkillVersionError("Version must pass validation and tests before activation")
        active = self.ensure_active_version(skill)
        if active.permission_fingerprint != version.permission_fingerprint:
            request = self._latest_version_permission_request(skill, version)
            if request is None or request.status != "approved":
                self.create_runtime_request_if_needed(skill, version)
                raise SkillVersionError("Runtime permission approval is required before activating this version")

        try:
            with SkillOperationGuard(self.db).locked(skill, "update", reason=f"Activating {version.version}"):
                if active.id != version.id:
                    active.status = "archived"
                version.status = "active"
                version.activated_at = utc_now()
                self._point_skill_at_version(skill, version)
                self.db.commit()
                self.db.refresh(skill)
                return skill
        except SkillOperationConflict as exc:
            raise SkillVersionError(str(exc)) from exc

    def discard_version(self, skill: Skill, version: SkillVersion) -> None:
        if version.skill_id != skill.id:
            raise SkillVersionError("Version does not belong to this skill")
        if version.status == "active" or skill.active_version_id == version.id:
            raise SkillVersionError("Active versions cannot be discarded")
        try:
            with SkillOperationGuard(self.db).locked(skill, "update", reason=f"Discarding {version.version}"):
                folder = self._version_dir(version)
                if folder.exists():
                    shutil.rmtree(folder)
                version.status = "discarded"
                self.db.commit()
        except SkillOperationConflict as exc:
            raise SkillVersionError(str(exc)) from exc

    def compare_with_active(self, skill: Skill, version: SkillVersion) -> dict[str, Any]:
        active = self.ensure_active_version(skill)
        active_dir = self._version_dir(active)
        candidate_dir = self._version_dir(version)
        files = []
        for relative_path in READABLE_COMPARE_FILES:
            files.append(
                {
                    "path": relative_path,
                    "active": self._read_optional(active_dir / relative_path),
                    "candidate": self._read_optional(candidate_dir / relative_path),
                }
            )
        return {"active_version": active, "candidate_version": version, "files": files}

    def permission_fingerprint(self, manifest_json: dict[str, Any]) -> str:
        payload = {
            "permissions": manifest_json.get("permissions", {}),
            "dependencies": manifest_json.get("dependencies", []),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _create_initial_version_from_folder(self, skill: Skill, source_dir: Path) -> SkillVersion:
        destination = self._versions_root(skill) / "v1"
        if source_dir.resolve() != destination.resolve():
            if not destination.exists():
                self._copy_skill_folder(source_dir, destination)
        manifest = validate_manifest_file(destination / "manifest.json")
        version = SkillVersion(
            skill_id=skill.id,
            version="v1",
            status="active",
            folder_path=self._relative_path(destination),
            code_snapshot_path=self._relative_path(destination),
            manifest_json=manifest.model_dump(mode="json"),
            change_summary="Initial active version.",
            changelog="Initial active version.",
            created_by="system",
            permission_fingerprint=self.permission_fingerprint(manifest.model_dump(mode="json")),
            test_status="not_run",
            validation_status="passed",
            activated_at=utc_now(),
        )
        self.db.add(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def _point_skill_at_version(self, skill: Skill, version: SkillVersion) -> None:
        folder = self._version_dir(version)
        manifest = validate_manifest_file(folder / "manifest.json")
        skill.active_version_id = version.id
        skill.installed_path = version.folder_path
        skill.manifest_path = self._relative_path(folder / "manifest.json")
        skill.description = manifest.description
        skill.skill_type = manifest.skill_type
        skill.interface_type = manifest.interface_type
        skill.risk_level = manifest.risk_level
        skill.instructions_path = manifest.instructions_path
        skill.input_schema_json = manifest.input_schema
        skill.output_schema_json = manifest.output_schema
        skill.tool_ui_schema_json = manifest.tool_ui_schema
        self.db.commit()
        self.db.refresh(skill)

    def _non_discarded_versions(self, skill: Skill) -> list[SkillVersion]:
        return list(
            self.db.scalars(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill.id)
                .where(SkillVersion.status != "discarded")
                .order_by(SkillVersion.id.asc())
            ).all()
        )

    def _next_version_number(self, versions: list[SkillVersion]) -> int:
        numbers = []
        for version in versions:
            if version.version.startswith("v") and version.version[1:].isdigit():
                numbers.append(int(version.version[1:]))
        return max(numbers, default=0) + 1

    def _versions_root(self, skill: Skill) -> Path:
        safe_name = self.proposed_service.validate_skill_name(skill.name)
        return self.project_root / "skills" / "installed" / safe_name / "versions"

    def _version_dir(self, version: SkillVersion) -> Path:
        path = Path(version.folder_path)
        if path.is_absolute():
            raise SkillVersionError("Version folder paths must be relative")
        resolved = (self.project_root / path).resolve()
        installed_root = (self.project_root / "skills" / "installed").resolve()
        if not resolved.is_relative_to(installed_root):
            raise SkillVersionError("Version folder must stay inside skills/installed")
        return resolved

    def _copy_skill_folder(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        for child in source.iterdir():
            if child.name in {"versions", "__pycache__", ".pytest_cache"}:
                continue
            target = destination / child.name
            if child.is_dir():
                shutil.copytree(child, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
            else:
                shutil.copy2(child, target)

    def _relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.project_root).as_posix()

    def _read_optional(self, path: Path) -> str | None:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def _latest_version_permission_request(self, skill: Skill, version: SkillVersion) -> ApprovalRequest | None:
        requests = self.db.scalars(
            select(ApprovalRequest)
            .where(ApprovalRequest.skill_id == skill.id)
            .where(ApprovalRequest.request_scope == "runtime")
            .where(ApprovalRequest.request_type == "install")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        ).all()
        for request in requests:
            if (request.reason_json or {}).get("version_id") == version.id:
                return request
        return None
