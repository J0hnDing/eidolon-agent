import os
from pathlib import Path

MAX_READABLE_FILES = 200
MAX_SNAPSHOT_CHARACTERS = 12_000
READABLE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
EXCLUDED_PARTS = {
    ".agents",
    ".deps",
    ".git",
    ".pytest_cache",
    ".pytest_tmp",
    "__pycache__",
    "cache",
    "node_modules",
}
EXCLUDED_NAMES = {
    "codex_last_message.txt",
    "codex_prompt.txt",
    "interface_artifact.json",
}


class SkillPackageFileError(ValueError):
    pass


def readable_skill_paths(root: Path) -> list[str]:
    resolved_root = root.resolve()
    paths: list[str] = []
    for path in iter_skill_files(resolved_root):
        if not _is_readable_path(resolved_root, path):
            continue
        paths.append(path.relative_to(resolved_root).as_posix())
        if len(paths) >= MAX_READABLE_FILES:
            break
    return paths


def iter_skill_files(root: Path) -> list[Path]:
    """Enumerate package files without descending into transient or inaccessible trees."""
    resolved_root = root.resolve()
    paths: list[Path] = []
    for current, directory_names, file_names in os.walk(resolved_root, topdown=True, onerror=lambda _error: None):
        directory_names[:] = sorted(
            name for name in directory_names if name not in EXCLUDED_PARTS and not name.startswith(".")
        )
        current_path = Path(current)
        paths.extend(current_path / name for name in sorted(file_names) if not name.startswith("."))
    return paths


def read_skill_text(root: Path, relative_path: str, *, max_characters: int | None = None) -> str:
    resolved_root = root.resolve()
    normalized = relative_path.replace("\\", "/").removeprefix("./")
    candidate = (resolved_root / normalized).resolve()
    if not candidate.is_relative_to(resolved_root) or not _is_readable_path(resolved_root, candidate):
        raise SkillPackageFileError("Requested file is not readable as a skill package file")
    if not candidate.is_file():
        raise SkillPackageFileError("Requested file does not exist")
    content = candidate.read_text(encoding="utf-8")
    return content if max_characters is None else content[:max_characters]


def snapshot_skill_files(root: Path, *, max_characters: int = MAX_SNAPSHOT_CHARACTERS) -> dict[str, str]:
    return {
        relative_path: read_skill_text(root, relative_path, max_characters=max_characters)
        for relative_path in readable_skill_paths(root)
    }


def _is_readable_path(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if not relative.parts or any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.name in EXCLUDED_NAMES:
        return False
    return path.suffix.lower() in READABLE_SUFFIXES
