import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import SkillRun
from app.schemas.manifest import SkillManifest
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file


DEFAULT_TIMEOUT_SECONDS = 10


class UnsupportedSkillPermissionError(ValueError):
    pass


class RunAlreadyFinalized(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_supported_permissions(manifest: SkillManifest) -> None:
    permissions = manifest.permissions
    if permissions.network:
        raise UnsupportedSkillPermissionError("network permissions are not supported in Milestone 3")
    if permissions.filesystem_read:
        raise UnsupportedSkillPermissionError("filesystem read permissions are not supported in Milestone 3")
    if permissions.secrets:
        raise UnsupportedSkillPermissionError("secret permissions are not supported in Milestone 3")
    if permissions.shell:
        raise UnsupportedSkillPermissionError("shell permissions are not supported in Milestone 3")

    allowed_writes = {"cache"}
    requested_writes = {
        path.replace("\\", "/").removeprefix("./").rstrip("/")
        for path in permissions.filesystem_write
    }
    if not requested_writes.issubset(allowed_writes):
        raise UnsupportedSkillPermissionError("filesystem writes are limited to ./cache in Milestone 3")


class SkillRunner:
    def __init__(self, db: Session, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.db = db
        self.timeout_seconds = timeout_seconds

    def run(self, skill_id: int, skill_dir: Path, input_json: dict[str, Any]) -> SkillRun:
        run = SkillRun(
            skill_id=skill_id,
            status="running",
            input_json=input_json,
            started_at=utc_now(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        skill_dir = skill_dir.resolve()
        try:
            manifest = self._load_manifest(skill_dir)
            if manifest.skill_type == "instruction":
                raise UnsupportedSkillPermissionError("instruction skills cannot be executed")
            validate_supported_permissions(manifest)
            entrypoint = self._resolve_entrypoint(skill_dir, manifest.entrypoint)
            self._run_tests(skill_dir, run)
            self._run_entrypoint(entrypoint, skill_dir, input_json, run)
        except (ManifestValidationError, UnsupportedSkillPermissionError, FileNotFoundError) as exc:
            self._finish_run(run, status="blocked", error_message=str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            self._finish_run(
                run,
                status="failed",
                stdout=stdout,
                stderr=stderr,
                error_message=f"Skill timed out after {self.timeout_seconds} seconds",
            )
        except RunAlreadyFinalized:
            pass
        except Exception as exc:
            self._finish_run(run, status="failed", error_message=str(exc))

        self.db.refresh(run)
        return run

    def _load_manifest(self, skill_dir: Path) -> SkillManifest:
        manifest_path = skill_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found at {manifest_path}")
        return validate_manifest_file(manifest_path)

    def _resolve_entrypoint(self, skill_dir: Path, entrypoint: str | None) -> Path:
        if entrypoint is None:
            raise FileNotFoundError("Executable skills require an entrypoint")
        resolved = (skill_dir / entrypoint).resolve()
        if not resolved.is_relative_to(skill_dir):
            raise FileNotFoundError("Skill entrypoint must stay inside the skill directory")
        if not resolved.exists():
            raise FileNotFoundError(f"Skill entrypoint not found at {resolved}")
        return resolved

    def _run_tests(self, skill_dir: Path, run: SkillRun) -> None:
        tests_dir = skill_dir / "tests"
        if not tests_dir.exists():
            self._finish_run(run, status="blocked", error_message="Skill tests directory is missing")
            raise RunAlreadyFinalized("Skill tests directory is missing")

        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_dir)],
            cwd=skill_dir,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )
        if result.returncode != 0:
            self._finish_run(
                run,
                status="blocked",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill tests failed; entrypoint was not run",
            )
            raise RunAlreadyFinalized("Skill tests failed; entrypoint was not run")

    def _run_entrypoint(
        self,
        entrypoint: Path,
        skill_dir: Path,
        input_json: dict[str, Any],
        run: SkillRun,
    ) -> None:
        result = subprocess.run(
            [sys.executable, str(entrypoint)],
            cwd=skill_dir,
            input=json.dumps(input_json),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )

        if result.returncode != 0:
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill exited with a non-zero status",
            )
            return

        try:
            output_json = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message=f"Skill stdout was not valid JSON: {exc.msg}",
            )
            return

        if not isinstance(output_json, dict):
            self._finish_run(
                run,
                status="failed",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                error_message="Skill stdout JSON must be an object",
            )
            return

        self._finish_run(
            run,
            status="succeeded",
            output_json=output_json,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
        )

    def _finish_run(
        self,
        run: SkillRun,
        status: str,
        output_json: dict[str, Any] | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
        error_message: str | None = None,
    ) -> None:
        run.status = status
        if output_json is not None:
            run.output_json = output_json
        if stdout is not None:
            run.stdout = stdout
        if stderr is not None:
            run.stderr = stderr
        if exit_code is not None:
            run.exit_code = exit_code
        if error_message is not None:
            run.error_message = error_message
        run.ended_at = utc_now()
        self.db.commit()
