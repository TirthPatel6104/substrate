"""Configuration and workspace/database path resolution."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Base directory for Substrate state (respects SUBSTRATE_HOME / XDG)."""
    override = os.environ.get("SUBSTRATE_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "substrate"


def db_path_for(workspace: str | None) -> Path:
    """Return the DB file for a workspace (or the global DB when None)."""
    root = data_dir()
    if not workspace or workspace == "global":
        return root / "global.substrate.db"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in workspace)
    return root / "workspaces" / f"{safe}.substrate.db"


def workspace_id(path: str | None = None) -> str:
    """Derive a stable workspace id from a path (defaults to cwd)."""
    p = Path(path).expanduser().resolve() if path else Path.cwd()
    return p.name or "global"
