"""Which checkout a hook event concerns.

Claude Code runs a hook wherever the *session* is, not where the project is:

- The hook process runs in the session's current directory. That directory
  moves when the session runs ``cd`` inside its project or enters a worktree,
  so it can be a subfolder or a worktree rather than the project root.
- In a worktree session started from the desktop app, the hook process runs
  in the main checkout, and the worktree being edited arrives only as the
  ``cwd`` field of the hook's input.
- ``CLAUDE_PROJECT_DIR`` names the directory the session started in, and it
  never moves.

So a hook works out its checkout from the event rather than from its own
working directory: the input's ``cwd`` for a session-level event (Stop,
SessionStart), the edited file for a write. It uses that checkout only when
it belongs to the same repository as the session's project, where a worktree
counts as the same repository. Otherwise a session-level hook checks the
project itself, and a write hook leaves the file alone: another repository's
``kb/`` is not this project's to validate.

Without ``CLAUDE_PROJECT_DIR``, for example under another harness, the
session's current directory stands in for the project, which matches how the
hooks behaved before this module existed.

Repository identity is read from the ``.git`` entries directly, without
running git, because ``kb_write_guard`` runs on every file edit.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from aexp.utils.paths import RepoRootNotFound, find_repo_root

PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"


def read_stdin() -> str:
    """Read the hook payload, or ``""`` when there is no usable stdin."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except Exception:
        return ""


def read_event(raw: str) -> dict[str, Any]:
    """Parse a hook's stdin payload. An empty or malformed payload is ``{}``."""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def event_cwd(event: dict[str, Any]) -> Path:
    """The session's current directory: the input's ``cwd``, else the process's."""
    cwd = event.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


def project_dir(event: dict[str, Any]) -> Path:
    """The directory the session started in (``CLAUDE_PROJECT_DIR``)."""
    env = os.environ.get(PROJECT_DIR_ENV, "")
    return Path(env) if env else event_cwd(event)


def file_path_for(fp: str, event: dict[str, Any]) -> Path:
    """An edited file's absolute path. A relative one is taken from the session's cwd."""
    path = Path(fp)
    return path if path.is_absolute() else event_cwd(event) / path


def is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or lies under it (case-insensitive on Windows)."""
    p = os.path.normcase(str(path.resolve()))
    r = os.path.normcase(str(root.resolve()))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def checkout_for(location: Path, event: dict[str, Any]) -> Path | None:
    """The checkout ``location`` belongs to, when it is part of this session's repository.

    Returns ``None`` when ``location`` lies in another repository, or outside
    every repository and outside the project.
    """
    project_root = _project_root(event)
    root = _repo_root(location)
    if root is None:
        return project_root if is_within(location, project_root) else None
    return root if _same_repository(root, project_root) else None


def session_checkout(event: dict[str, Any]) -> Path:
    """The checkout a session-level event concerns.

    The checkout holding the session's current directory when it belongs to
    this session's repository, a worktree included. Otherwise the project.
    """
    return checkout_for(event_cwd(event), event) or _project_root(event)


def _project_root(event: dict[str, Any]) -> Path:
    project = project_dir(event)
    return _repo_root(project) or project.resolve()


def _repo_root(path: Path) -> Path | None:
    try:
        return find_repo_root(path)
    except RepoRootNotFound:
        return None


def _same_repository(a: Path, b: Path) -> bool:
    if _same_path(a, b):
        return True
    common_a, common_b = _git_common_dir(a), _git_common_dir(b)
    return common_a is not None and common_b is not None and _same_path(common_a, common_b)


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _git_common_dir(root: Path) -> Path | None:
    """The ``.git`` directory a checkout shares with every worktree of its repository.

    A main checkout's ``.git`` is that directory. A linked worktree's ``.git``
    is a file naming its private git directory, whose ``commondir`` file
    points back at the shared one.
    """
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git.resolve()
    if not dot_git.is_file():
        return None
    try:
        first_line = dot_git.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first_line.startswith("gitdir:"):
        return None
    git_dir = Path(first_line[len("gitdir:"):].strip())
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    try:
        common = Path((git_dir / "commondir").read_text(encoding="utf-8").strip())
    except OSError:
        return git_dir.resolve()
    if not common.is_absolute():
        common = git_dir / common
    return common.resolve()
