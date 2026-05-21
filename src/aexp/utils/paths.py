"""Repo-root discovery and install-marker bookkeeping.

``find_repo_root`` walks upward from a starting directory looking for a
``.git`` folder (or the explicit ``.aexp/installed.json``
marker). ``resolve_run_store_path`` returns the absolute path to the
signac project root, reading ``.aexp/installed.json`` if
present, otherwise defaulting to ``.runs/``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from aexp.utils.atomic import atomic_write

INSTALLED_MARKER_REL = Path(".aexp") / "installed.json"
DEFAULT_RUN_STORE = ".runs"


class InstalledMarker(TypedDict, total=False):
    """Schema of the on-disk ``.aexp/installed.json`` marker."""

    version: str
    installed_at: str
    run_store_path: str
    vendor_sha: str
    python_exe: str           # absolute path to the Python that ran install_scaffold
    conda_env_name: str       # CONDA_DEFAULT_ENV at install time, or "" for venv/system Python
    jupyter_enabled: bool     # True if any prior install used --with-jupyter; sticky once set


class RepoRootNotFound(RuntimeError):
    """Raised when ``find_repo_root`` walks to the filesystem root without finding one."""


def find_repo_root(start: str | Path | None = None) -> Path:
    """Find the enclosing git repo root.

    Walks upward from ``start`` looking for either a ``.git`` directory
    (or file, for submodules and worktrees) or an existing
    ``.aexp/installed.json`` marker. Either is treated as
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
    """Read ``.aexp/installed.json`` if it exists."""
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
    vendor_sha: str,
    installed_at: str | None = None,
    python_exe: str | None = None,
    conda_env_name: str | None = None,
    jupyter_enabled: bool | None = None,
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
    vendor_sha : str
        Fingerprint of the vendored research-harness snapshot used at install time.
    installed_at : str or None
        ISO-8601 UTC timestamp. Defaults to ``now`` in UTC.
    python_exe : str or None
        Absolute path to the Python interpreter that has this package
        importable. Defaults to ``sys.executable`` at call time. Slash
        commands + docs use this as the "known-good" invocation path,
        so the value should point at whatever interpreter can do
        ``python -m aexp``.
    conda_env_name : str or None
        Name of the conda env active at install time, or ``""`` if
        Python is a venv / system install. Defaults to reading
        ``CONDA_DEFAULT_ENV`` from the process environment.
    jupyter_enabled : bool or None
        Sticky-true marker recording whether this consumer has ever opted
        into the Jupyter MCP integration via ``aexp install --with-jupyter``.
        Pass ``True`` to set; ``False``/``None`` preserves whatever the
        previous marker had. The field is never auto-cleared — backing out
        of the integration is a manual edit by the user.

    Returns
    -------
    Path
        The absolute path of the written marker.
    """
    import sys

    if installed_at is None:
        installed_at = datetime.now(UTC).isoformat(timespec="seconds")
    if python_exe is None:
        python_exe = sys.executable
    if conda_env_name is None:
        conda_env_name = _detect_conda_env_name(python_exe) or ""

    payload: InstalledMarker = {
        "version": version,
        "installed_at": installed_at,
        "run_store_path": run_store_path,
        "vendor_sha": vendor_sha,
        "python_exe": python_exe,
        "conda_env_name": conda_env_name,
    }

    # jupyter_enabled is sticky-true: once set, it persists across re-installs
    # even if the caller doesn't pass --with-jupyter. We carry forward the
    # previous marker's value, then OR in whatever this call requested.
    prev = read_installed_marker(repo_root) or {}
    prev_enabled = bool(prev.get("jupyter_enabled", False))
    new_enabled = prev_enabled or bool(jupyter_enabled)
    if new_enabled:
        payload["jupyter_enabled"] = True

    target = Path(repo_root) / INSTALLED_MARKER_REL
    atomic_write(target, json.dumps(payload, indent=2) + "\n")
    return target


def _detect_conda_env_name(python_exe: str | None = None) -> str:
    """Best-effort conda-env name detection.

    Strategy:

    1. ``CONDA_DEFAULT_ENV`` env var — set when the install was run from
       a shell that invoked ``conda activate``.
    2. Parse the Python path for ``/envs/<name>/`` or ``\\envs\\<name>\\``
       segments — works when the env's Python was invoked directly (e.g.
       Poetry running inside a conda env without explicit activation).

    Returns an empty string if neither yields a name.
    """
    import os
    import sys

    env = os.environ.get("CONDA_DEFAULT_ENV", "") or ""
    if env and env != "base":
        return env
    exe = python_exe or sys.executable
    # Look for .../envs/<name>/... anywhere in the path. Split on both
    # separators so a Windows-style path parses correctly on Linux (and vice
    # versa) — `pathlib.Path` uses the host OS's rules and won't split on
    # the foreign separator.
    import re

    parts = [p for p in re.split(r"[\\/]", exe) if p]
    for i, part in enumerate(parts):
        if part == "envs" and i + 1 < len(parts):
            candidate = parts[i + 1]
            # Skip "envs/bin" or similar non-env segments — the thing after
            # "envs" should look like an environment name, not a system dir.
            if candidate and candidate not in {"bin", "Scripts"}:
                return candidate
    return ""


def resolve_invocation(repo_root: str | Path) -> list[str]:
    """Return the argv prefix that reliably invokes ``aexp``.

    The invocation strategy (plan §cross-platform-shell):

    1. If the install marker records a ``conda_env_name``, prefer
       ``conda run -n <env> python -m aexp`` — works from any
       shell where ``conda`` is on PATH, no activation required.
    2. Otherwise fall back to ``<python_exe> -m aexp`` using
       the absolute Python path captured at install time — works for venv
       users and any context where ``python`` alone wouldn't resolve to the
       env with this package installed.
    3. As a last resort (marker missing / malformed), return
       ``[sys.executable, "-m", "aexp"]``.

    Returns
    -------
    list[str]
        Argv prefix to extend with CLI verb + args. Example:
        ``["conda", "run", "-n", "agentic-exp", "python", "-m", "aexp"]``
    """
    import sys

    root = Path(repo_root)
    marker = read_installed_marker(root)
    if marker:
        env = (marker.get("conda_env_name") or "").strip()
        if env:
            return ["conda", "run", "-n", env, "python", "-m", "aexp"]
        python_exe = (marker.get("python_exe") or "").strip()
        if python_exe:
            return [python_exe, "-m", "aexp"]
    return [sys.executable, "-m", "aexp"]


def resolve_run_store_path(repo_root: str | Path) -> Path:
    """Return the absolute path to the signac run store for a repo.

    Reads ``run_store_path`` from the install marker if present, otherwise
    falls back to ``<repo_root>/.runs``.
    """
    root = Path(repo_root)
    marker = read_installed_marker(root)
    rel = marker["run_store_path"] if marker and "run_store_path" in marker else DEFAULT_RUN_STORE
    return (root / rel).resolve()
