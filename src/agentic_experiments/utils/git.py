"""Git provenance helpers.

Reads commit hash, dirty flag, and branch from a git working tree via
``git`` subprocesses. Falls back to ``{"commit": "", "dirty": False,
"branch": ""}`` when git isn't available or the directory is not a repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TypedDict


class GitProvenance(TypedDict):
    """Git state for a working tree at one point in time."""

    commit: str
    dirty: bool
    branch: str


def _run_git(args: list[str], cwd: Path) -> str:
    """Run ``git <args>`` in ``cwd``. Return stripped stdout or ``""``."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def get_git_provenance(cwd: str | Path | None = None) -> GitProvenance:
    """Return the current commit, dirty flag, and branch for ``cwd``.

    Parameters
    ----------
    cwd : str, Path, or None
        Working directory to query. Defaults to the current directory.

    Returns
    -------
    GitProvenance
        ``{"commit": str, "dirty": bool, "branch": str}``.
        Empty strings and ``False`` are returned when git isn't available
        or the directory is not inside a repo.
    """
    directory = Path(cwd) if cwd is not None else Path.cwd()

    commit = _run_git(["rev-parse", "HEAD"], directory)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], directory)
    dirty_status = _run_git(["status", "--porcelain"], directory)

    return GitProvenance(
        commit=commit,
        dirty=bool(dirty_status),
        branch=branch if branch != "HEAD" else "",
    )
