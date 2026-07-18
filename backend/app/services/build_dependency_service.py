import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApprovalRequest, Skill, SkillGenerationRequest
from app.services.default_permissions import default_build_time_dependencies
from app.services.dependency_environment import (
    DEPENDENCY_STATE_FILE,
    DependencyRequirementError,
    normalize_requirements,
    read_dependency_state,
    requirements_satisfied,
)
from app.services.proposed_skill_service import ProposedSkillService

PLATFORM_BUILD_REQUIREMENTS = ("pytest",)
DEFAULT_INSTALL_TIMEOUT_SECONDS = 300


class BuildDependencyError(ValueError):
    pass


@dataclass(frozen=True)
class BuildDependencyProvisioningResult:
    runtime_dependencies: list[str]
    build_dependencies: list[str]
    installed_runtime_dependencies: list[str]
    installed_build_dependencies: list[str]
    reused_runtime_dependencies: bool
    reused_build_dependencies: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_dependencies": self.runtime_dependencies,
            "build_dependencies": self.build_dependencies,
            "installed_runtime_dependencies": self.installed_runtime_dependencies,
            "installed_build_dependencies": self.installed_build_dependencies,
            "reused_runtime_dependencies": self.reused_runtime_dependencies,
            "reused_build_dependencies": self.reused_build_dependencies,
        }


@dataclass
class BuildDependencyService:
    db: Session
    project_root: Path | None = None
    timeout_seconds: int = DEFAULT_INSTALL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self.proposed_service = ProposedSkillService(self.db, project_root=self.project_root)

    def provision(
        self,
        generation_request: SkillGenerationRequest,
        skill: Skill,
        permission_plan: dict[str, object],
    ) -> BuildDependencyProvisioningResult:
        approval = self._approved_build_request(generation_request)
        runtime = permission_plan.get("runtime") if isinstance(permission_plan.get("runtime"), dict) else {}
        build_time = (
            permission_plan.get("build_time") if isinstance(permission_plan.get("build_time"), dict) else {}
        )
        try:
            runtime_dependencies = normalize_requirements(runtime.get("dependencies", []))
            effective_build_dependencies = normalize_requirements(build_time.get("dependencies", []))
        except DependencyRequirementError as exc:
            raise BuildDependencyError(str(exc)) from exc
        default_build_dependencies = {
            canonicalize_name(Requirement(dependency).name) for dependency in default_build_time_dependencies()
        }
        build_dependencies = [
            dependency
            for dependency in effective_build_dependencies
            if canonicalize_name(Requirement(dependency).name) not in default_build_dependencies
            and dependency not in runtime_dependencies
        ]
        approved_dependencies = set(normalize_requirements(approval.requested_dependencies_json or []))
        unapproved = sorted(set(runtime_dependencies + build_dependencies) - approved_dependencies)
        if unapproved:
            raise BuildDependencyError(
                "Build dependency approval is missing for: "
                + ", ".join(unapproved)
                + ". Approve the dependency before the build starts."
            )

        skill_dir = self.proposed_service.skill_dir_for_record(skill)
        if not skill_dir.is_dir():
            raise BuildDependencyError(f"Controlled build workspace does not exist: {skill_dir}")

        installed_runtime, reused_runtime = self._ensure_target(
            skill_dir / ".deps",
            runtime_dependencies,
        )
        requested_build_environment = normalize_requirements(
            [*PLATFORM_BUILD_REQUIREMENTS, *build_dependencies]
        )
        locally_required_build_dependencies = self._missing_requirements(
            requested_build_environment,
        )
        installed_build, reused_build = self._ensure_target(
            skill_dir / ".build-deps",
            locally_required_build_dependencies,
        )
        if not requirements_satisfied(
            requested_build_environment,
            paths=[skill_dir / ".build-deps"],
            include_global_environment=True,
        ):
            raise BuildDependencyError(
                "The controlled build environment could not verify required build tooling: "
                + ", ".join(requested_build_environment)
            )
        return BuildDependencyProvisioningResult(
            runtime_dependencies=runtime_dependencies,
            build_dependencies=requested_build_environment,
            installed_runtime_dependencies=installed_runtime,
            installed_build_dependencies=installed_build,
            reused_runtime_dependencies=reused_runtime,
            reused_build_dependencies=reused_build,
        )

    def _approved_build_request(self, generation_request: SkillGenerationRequest) -> ApprovalRequest:
        approval = self.db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.generation_request_id == generation_request.id)
            .where(ApprovalRequest.request_scope == "build_time")
            .where(ApprovalRequest.status == "approved")
            .order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id.desc())
        )
        if approval is None:
            raise BuildDependencyError("Build-time dependency approval is missing")
        return approval

    def _ensure_target(
        self,
        target: Path,
        requirements: list[str],
    ) -> tuple[list[str], bool]:
        if not requirements:
            self._remove_tree(target)
            return [], False
        state_path = target / DEPENDENCY_STATE_FILE
        state = read_dependency_state(state_path)
        if state == requirements and requirements_satisfied(
            requirements,
            paths=[target],
            include_global_environment=False,
        ):
            return [], True

        staging = target.parent / f".{target.name}-staging-{uuid4().hex}"
        backup = target.parent / f".{target.name}-backup-{uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--target",
                    str(staging),
                    *requirements,
                ],
                cwd=target.parent,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                shell=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "pip exited without a diagnostic"
                raise BuildDependencyError(f"Could not install approved build dependencies: {detail}")
            if not requirements_satisfied(
                requirements,
                paths=[staging],
                include_global_environment=False,
            ):
                raise BuildDependencyError(
                    "Installed dependencies could not be verified in the controlled dependency folder: "
                    + ", ".join(requirements)
                )
            (staging / DEPENDENCY_STATE_FILE).write_text(
                json.dumps(requirements, indent=2) + "\n",
                encoding="utf-8",
            )
            if target.exists():
                os.replace(target, backup)
            os.replace(staging, target)
        except (OSError, subprocess.SubprocessError) as exc:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise BuildDependencyError(f"Could not provision the controlled build environment: {exc}") from exc
        finally:
            self._remove_tree(staging)
            if target.exists():
                self._remove_tree(backup)
        return list(requirements), False

    def _missing_requirements(
        self,
        requirements: list[str],
    ) -> list[str]:
        return [
            requirement
            for requirement in requirements
            if not requirements_satisfied(
                [requirement],
                paths=[],
                include_global_environment=True,
            )
        ]

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
