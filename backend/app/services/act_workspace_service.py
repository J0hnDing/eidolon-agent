from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ACT_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "act"


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
