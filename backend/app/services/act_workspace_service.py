from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

ACT_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "act"

AGENT_INSTRUCTIONS = """# Eidolon Act

This directory is managed by Eidolon. The persistent writable working directory is
`workspace/`; `memory/` is reserved and intentionally empty in this version.

Act must:

- use Eidolon MCP tools as its primary capabilities;
- write files only in the writable workspace supplied by Eidolon;
- use `act.document.download` for remote documents and images;
- never read or modify credentials, Eidolon application source, `AGENTS.md`, or `memory/`;
- keep user-visible results concise and state what changed.
"""


class ActWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActWorkspace:
    root: Path
    memory: Path
    workspace: Path
    downloads: Path
    instructions: Path


def ensure_act_workspace() -> ActWorkspace:
    root = ACT_ROOT.resolve()
    memory = root / "memory"
    workspace = root / "workspace"
    downloads = workspace / "downloads"
    instructions = root / "AGENTS.md"
    memory.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    if any(memory.iterdir()):
        raise ActWorkspaceError(
            "Act memory must remain empty in this version. Remove its contents before starting Act."
        )
    _write_managed_file(instructions, AGENT_INSTRUCTIONS)
    return ActWorkspace(
        root=root,
        memory=memory,
        workspace=workspace,
        downloads=downloads,
        instructions=instructions,
    )


def _write_managed_file(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".AGENTS.", suffix=".tmp", delete=False) as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
