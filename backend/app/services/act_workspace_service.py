from __future__ import annotations

import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ACT_ROOT = Path.home() / ".eidolon" / "agents"
_MEMORY_LOCK = threading.RLock()
MEMORY_MAP_START = "<!-- eidolon:memory-map:start -->"
MEMORY_MAP_END = "<!-- eidolon:memory-map:end -->"
_LEGACY_MEMORY_GUIDANCE = (
    "- `memory/` is for small, explicit, persistent memories that the user requested or\n"
    "  approved. Never store transcripts, credentials, or silent observations there."
)
MEMORY_MAP = """## Shared agent memory

All durable agent memory is stored under `memory/`. Atlas remains the authority
for structured personal knowledge. There are no separate database Mem facts or
continuity-summary stores. Read the relevant files when needed; do not assume
that workspace files are automatically inserted into model context.

- `memory/facts-outside-atlas/`: editable JSON facts that do not fit Atlas.
  Preserve IDs, provenance, and timestamps when correcting these facts. No
  working-preferences or conventions categories have been defined yet.
- `memory/daily-summaries/YYYY-MM-DD.md`: very short, concise reflection notes
  recording important things done or decisions made that day, if any. Mandatory
  end-of-day reflection writes this summary; it is not a chat transcript.
  `legacy-continuity.md`, when present, preserves the former continuity note.
- `memory/chat-history/{assistant,act,observer}/`: complete retained user and
  agent messages, including live replies, with timestamps and session/turn IDs.
  Search these only when actual conversation evidence is needed. History is
  bounded to 30 days and 50 MiB in total; oldest whole messages are discarded.
  Internal activation prompts, tool traces, and private reasoning are excluded.
  This is the only store for completed message content. SQLite retains active
  messages until archival, then erases their text and keeps runtime metadata.
  Retention removes expired messages from the UI and recovery paths too.

The Assistant agenda remains in Eidolon's agenda tools/database; use those tools
for responsibilities, questions, status, and scheduling. Do not duplicate it in
memory files. `knowledge/` contains reference material and `workspace/` contains
working files. Files beginning with `.agent-memory` are backend bookkeeping;
leave them alone. Fresh daily Assistant sessions do not receive old transcripts
automatically; explicitly read a relevant memory file when needed.
"""


class ActWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActWorkspace:
    root: Path
    memory: Path
    knowledge: Path
    quercus: Path
    workspace: Path
    downloads: Path


def checked_workspace_path(root: Path, path: Path) -> Path:
    """Refuse links/junctions below the configured root, including owned files."""
    root = root.resolve()
    try:
        parts = path.absolute().relative_to(root).parts
    except ValueError:
        raise ActWorkspaceError("Memory path must remain inside the agent workspace") from None
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ActWorkspaceError("Agent memory paths cannot contain symbolic links or junctions")
    if not path.resolve().is_relative_to(root):
        raise ActWorkspaceError("Memory path must remain inside the agent workspace")
    return path


@contextmanager
def agent_memory_lock(root: Path):
    """Serialize backend and MCP file mutations, including Windows processes."""
    with _MEMORY_LOCK:
        path = checked_workspace_path(root, root / ".agent-memory.lock")
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_workspace_write(root: Path, path: Path, content: str) -> None:
    checked_workspace_path(root, path)
    descriptor, temporary = tempfile.mkstemp(prefix=".agent-memory-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        checked_workspace_path(root, path)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def ensure_agent_memory_layout(root: Path) -> None:
    root = root.resolve()
    with agent_memory_lock(root):
        for relative in ("memory/facts-outside-atlas", "memory/daily-summaries",
                         "memory/chat-history/assistant", "memory/chat-history/act", "memory/chat-history/observer"):
            checked_workspace_path(root, root / relative).mkdir(parents=True, exist_ok=True)
        agents_path = checked_workspace_path(root, root / "AGENTS.md")
        if agents_path.exists() and agents_path.stat().st_size > 1024 * 1024:
            raise ActWorkspaceError("Workspace AGENTS.md exceeds the supported size")
        original = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
        guidance = original.replace(
            _LEGACY_MEMORY_GUIDANCE,
            "- `memory/` holds editable facts, concise daily summaries and bounded full chat history\n"
            "  as documented below. Never store credentials there.",
        )
        section = f"{MEMORY_MAP_START}\n{MEMORY_MAP.rstrip()}\n{MEMORY_MAP_END}"
        if MEMORY_MAP_START in guidance:
            before, rest = guidance.split(MEMORY_MAP_START, 1)
            if MEMORY_MAP_END not in rest:
                raise ActWorkspaceError("Workspace AGENTS.md has an incomplete managed memory section")
            _, after = rest.split(MEMORY_MAP_END, 1)
            updated = before + section + after
        else:
            updated = guidance + ("\n\n" if guidance else "") + section + "\n"
        if updated != original:
            atomic_workspace_write(root, agents_path, updated)


def ensure_act_workspace() -> ActWorkspace:
    root = ACT_ROOT.resolve()
    memory = root / "memory"
    knowledge = root / "knowledge"
    quercus = knowledge / "quercus"
    workspace = root / "workspace"
    downloads = workspace / "downloads"
    memory.mkdir(parents=True, exist_ok=True)
    quercus.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    ensure_agent_memory_layout(root)
    return ActWorkspace(
        root=root,
        memory=memory,
        knowledge=knowledge,
        quercus=quercus,
        workspace=workspace,
        downloads=downloads,
    )


def open_act_root() -> None:
    workspace = ensure_act_workspace()
    if sys.platform != "win32":
        raise ActWorkspaceError("Opening the Act folder is supported only on Windows")
    try:
        os.startfile(workspace.root)  # type: ignore[attr-defined]
    except OSError:
        raise ActWorkspaceError("The Act folder could not be opened") from None
