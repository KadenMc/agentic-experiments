"""Repo-root discovery and install-marker bookkeeping.

``find_repo_root`` walks upward from a starting directory looking for a
``.git`` folder (or the explicit ``.agentic_experiments/installed.json``
marker). ``resolve_run_store_path`` returns the absolute path to the
signac project root, reading ``.agentic_experiments/installed.json`` if
present, otherwise defaulting to ``.runs/``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from agentic_experiments.utils.atomic import atomic_write

INSTALLED_MARKER_REL = Path(".agentic_experiments") / "installed.json"
DEFAULT_RUN_STORE = ".runs"


class InstalledMarker(TypedDict, total=False):
    """Schema of the on-disk ``.agentic_experiments/installed.json`` marker."""

    version: str
    installed_at: str
    run_store_path: str
    limina_vendor_sha: str


class RepoRootNotFound(RuntimeError):
    """Raised when ``find_repo_root`` walks to the filesystem root without finding one."""


def find_repo_root(start: str | Path | None = None) -> Path:
    """Find the enclosing git repo root.

    Walks upward from ``start`` looking for either a ``.git`` directory
    (or file, for submodules and worktrees) or an existing
    ``.agentic_experiments/installed.json`` marker. Either is treated as
    the repo root.

    Parameters
    ----------
    start : str, Path, or None
        Starting directory. Defaults to ``Path.cwd()``.

    Returns
    -------
    Path
        Absolute path to the detected repo root.

    Raises
    ------
    RepoRootNotFound
        If no marker is found before reaching the filesystem root.
    """
    here = Path(start).resolve() if start else Path.cwd().resolve()

    # `start` might itself be a file — climb to its parent in that case.
    if here.is_file():
        here = here.parent

    for candidate in (here, *here.parents):
        if (candidate / ".git").exists() or (candidate / INSTALLED_MARKER_REL).is_file():
            return candidate

    raise RepoRootNotFound(
        f"no .git directory or {INSTALLED_MARKER_REL} marker found above {here}"
    )


def read_installed_marker(repo_root: str | Path) -> InstalledMarker | None:
    """Read ``.agentic_experiments/installed.json`` if it exists."""
    marker = Path(repo_root) / INSTALLED_MARKER_REL
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data  # type: ignore[return-value]


def write_installed_marker(
    repo_root: str | Path,
    *,
    version: str,
    run_store_path: str,
    limina_vendor_sha: str,
    installed_at: str | None = None,
) -> Path:
    """Write a new install marker atomically.

    Parameters
    ----------
    repo_root : str or Path
        Repo root where the marker should live.
    version : str
        agentic-experiments package version.
    run_store_path : str
        Path (relative to ``repo_root``) of the signac project.
    limina_vendor_sha : str
        Fingerprint of the vendored Limina snapshot used at install time.
    installed_at : str or None
        ISO-8601 UTC timestamp. Defaults to ``now`` in UTC.

    Returns
    -------
    Path
        The absolute path of the written marker.
    """
    if installed_at is None:
        installed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    payload: InstalledMarker = {
        "version": version,
        "installed_at": installed_at,
        "run_store_path": run_store_path,
        "limina_vendor_sha": limina_vendor_sha,
    }
    target = Path(repo_root) / INSTALLED_MARKER_REL
    atomic_write(target, json.dumps(payload, indent=2) + "\n")
    return target


def resolve_run_store_path(repo_root: str | Path) -> Path:
    """Return the absolute path to the signac run store for a repo.

    Reads ``run_store_path`` from the install marker if present, otherwise
    falls back to ``<repo_root>/.runs``.
    """
    root = Path(repo_root)
    marker = read_installed_marker(root)
    rel = marker["run_store_path"] if marker and "run_store_path" in marker else DEFAULT_RUN_STORE
    return (root / rel).resolve()
