from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")


class CodexCliCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexCliCandidate:
    path: str
    source: str
    version: str | None
    version_tuple: tuple[int, int, int] | None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "source": self.source,
            "version": self.version,
            "error": self.error,
        }


@dataclass(frozen=True)
class CodexCliStatus:
    available: bool
    compatible: bool
    requested_command: str | None
    explicit_override: bool
    resolved_path: str | None
    source: str | None
    version: str | None
    minimum_version: str | None
    error: str | None
    candidates: tuple[CodexCliCandidate, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "compatible": self.compatible,
            "requested_command": self.requested_command,
            "explicit_override": self.explicit_override,
            "resolved_path": self.resolved_path,
            "source": self.source,
            "version": self.version,
            "minimum_version": self.minimum_version,
            "error": self.error,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


class CodexCliService:
    """Resolve and preflight the single Codex CLI used by every backend call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache_key: tuple[str | None, str | None, str, str, str] | None = None
        self._cached_status: CodexCliStatus | None = None

    def read_status(self, *, refresh: bool = False) -> dict[str, object]:
        return self.resolve(refresh=refresh).as_dict()

    def command(
        self,
        *,
        refresh: bool = False,
        minimum_version: str | None = None,
        capability: str = "the requested operation",
    ) -> str:
        status = self.resolve(refresh=refresh)
        if not status.available or not status.compatible or not status.resolved_path:
            raise CodexCliCompatibilityError(status.error or "A compatible Codex CLI is unavailable.")
        if minimum_version:
            required = self._parse_required_version(minimum_version)
            selected = self._parse_required_version(status.version)
            if required is None:
                raise ValueError("minimum_version must be a semantic version such as 0.140.0")
            if selected is None or selected < required:
                raise CodexCliCompatibilityError(
                    f"{capability} requires Codex CLI {minimum_version} or newer, but {status.version or 'an unknown version'} "
                    f"is resolved at '{status.resolved_path}'. Update Codex Desktop or set "
                    "PERSONAL_AGENT_CODEX_COMMAND to a compatible executable."
                )
        return status.resolved_path

    def resolve(self, *, refresh: bool = False) -> CodexCliStatus:
        override = self._normalized_env("PERSONAL_AGENT_CODEX_COMMAND")
        minimum_version = self._normalized_env("PERSONAL_AGENT_CODEX_MIN_VERSION")
        cache_key = (
            override,
            minimum_version,
            os.getenv("PATH", ""),
            os.getenv("LOCALAPPDATA", ""),
            os.getenv("ProgramFiles", ""),
        )
        with self._lock:
            if not refresh and cache_key == self._cache_key and self._cached_status is not None:
                return self._cached_status
            status = self._resolve_uncached(override=override, minimum_version=minimum_version)
            self._cache_key = cache_key
            self._cached_status = status
            return status

    def _resolve_uncached(self, *, override: str | None, minimum_version: str | None) -> CodexCliStatus:
        minimum_tuple = self._parse_required_version(minimum_version)
        if minimum_version and minimum_tuple is None:
            return CodexCliStatus(
                available=False,
                compatible=False,
                requested_command=override,
                explicit_override=override is not None,
                resolved_path=None,
                source=None,
                version=None,
                minimum_version=minimum_version,
                error=(
                    "PERSONAL_AGENT_CODEX_MIN_VERSION must be a semantic version such as 0.140.0."
                ),
            )

        paths = self._override_paths(override) if override else self._automatic_paths()
        candidates = tuple(self._probe(path, self._source(path, explicit=override is not None)) for path in paths)
        valid = [candidate for candidate in candidates if candidate.version_tuple is not None]
        selected = valid[0] if override and valid else max(
            valid,
            key=lambda candidate: (candidate.version_tuple or (0, 0, 0), self._source_priority(candidate.source)),
            default=None,
        )
        if selected is None:
            requested = override or "automatic discovery"
            detail = next((candidate.error for candidate in candidates if candidate.error), None)
            return CodexCliStatus(
                available=bool(candidates),
                compatible=False,
                requested_command=override,
                explicit_override=override is not None,
                resolved_path=candidates[0].path if candidates else None,
                source=candidates[0].source if candidates else None,
                version=None,
                minimum_version=minimum_version,
                error=(
                    f"Codex CLI '{requested}' could not be version-checked"
                    + (f": {detail}" if detail else ". Install or update Codex Desktop, or set PERSONAL_AGENT_CODEX_COMMAND.")
                ),
                candidates=candidates,
            )

        compatible = minimum_tuple is None or selected.version_tuple >= minimum_tuple
        error = None
        if not compatible:
            error = (
                f"Codex CLI {selected.version} at '{selected.path}' is older than the required "
                f"version {minimum_version}. Update Codex Desktop or set PERSONAL_AGENT_CODEX_COMMAND "
                "to a compatible executable."
            )
        return CodexCliStatus(
            available=True,
            compatible=compatible,
            requested_command=override,
            explicit_override=override is not None,
            resolved_path=selected.path,
            source=selected.source,
            version=selected.version,
            minimum_version=minimum_version,
            error=error,
            candidates=candidates,
        )

    def _automatic_paths(self) -> list[str]:
        candidates: list[str] = []
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            candidates.append(str(Path(local_app_data) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"))
        candidates.extend(self._path_candidates("codex"))
        return self._existing_unique(candidates)

    def _override_paths(self, override: str) -> list[str]:
        resolved = shutil.which(override)
        candidates = [resolved] if resolved else []
        candidate_path = Path(override).expanduser()
        if candidate_path.is_file():
            candidates.append(str(candidate_path.resolve()))
        return self._existing_unique(candidates)

    @staticmethod
    def _path_candidates(command: str) -> Iterable[str]:
        extensions = [""] if os.name != "nt" else [item.lower() for item in os.getenv("PATHEXT", ".EXE;.CMD;.BAT").split(";")]
        for raw_dir in os.getenv("PATH", "").split(os.pathsep):
            directory = raw_dir.strip().strip('"')
            if not directory:
                continue
            base = Path(directory) / command
            for extension in extensions:
                candidate = base if not extension else base.with_suffix(extension)
                try:
                    if candidate.is_file():
                        yield str(candidate.resolve())
                except OSError:
                    continue

    @staticmethod
    def _existing_unique(paths: Iterable[str | None]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if not path:
                continue
            try:
                resolved = str(Path(path).resolve())
                exists = Path(resolved).is_file()
            except OSError:
                continue
            key = os.path.normcase(resolved)
            if exists and key not in seen:
                seen.add(key)
                unique.append(resolved)
        return unique

    @staticmethod
    def _source(path: str, *, explicit: bool) -> str:
        if explicit:
            return "explicit_override"
        normalized = path.replace("/", "\\").lower()
        if "\\openai\\codex\\" in normalized or "\\windowsapps\\openai.codex_" in normalized:
            return "codex_desktop"
        return "path"

    @staticmethod
    def _source_priority(source: str) -> int:
        return {"explicit_override": 3, "codex_desktop": 2, "path": 1}.get(source, 0)

    @staticmethod
    def _probe(path: str, source: str) -> CodexCliCandidate:
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CodexCliCandidate(path=path, source=source, version=None, version_tuple=None, error=str(exc))
        output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        match = _VERSION_PATTERN.search(output)
        if result.returncode != 0 or match is None:
            error = output or f"version command exited with code {result.returncode}"
            return CodexCliCandidate(path=path, source=source, version=None, version_tuple=None, error=error)
        version_tuple = tuple(int(part) for part in match.groups())
        version = ".".join(str(part) for part in version_tuple)
        return CodexCliCandidate(path=path, source=source, version=version, version_tuple=version_tuple)

    @staticmethod
    def _parse_required_version(value: str | None) -> tuple[int, int, int] | None:
        if value is None:
            return None
        match = _VERSION_PATTERN.fullmatch(value.strip())
        return tuple(int(part) for part in match.groups()) if match else None

    @staticmethod
    def _normalized_env(name: str) -> str | None:
        value = os.getenv(name)
        return value.strip() if value and value.strip() else None


codex_cli_service = CodexCliService()


def should_use_real_codex() -> bool:
    mode = os.getenv("PERSONAL_AGENT_CODEX_MODE", "auto").strip().lower()
    if mode in {"fake", "dev", "stub", "local"}:
        return False
    status = codex_cli_service.resolve()
    if status.available:
        if not status.compatible:
            raise CodexCliCompatibilityError(status.error or "The resolved Codex CLI is incompatible.")
        return True
    if mode == "real":
        raise CodexCliCompatibilityError(status.error or "Codex real mode requires an installed Codex CLI.")
    return False
