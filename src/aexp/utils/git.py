"""Git provenance helpers.

Reads commit hash, dirty flag, and branch from a git working tree via
``git`` subprocesses. Falls back to ``{"commit": "", "dirty": False,
"branch": ""}`` when git isn't available or the directory is not a repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, TypedDict


class GitProvenance(TypedDict):
    """Git state for a working tree at one point in time."""

    commit: str
    dirty: bool
    branch: str


def _run_git(args: list[str], cwd: Path) -> str:
    """Run ``git <args>`` in ``cwd``. Return stripped stdout or ``""``.

    Always passes ``stdin=DEVNULL``. When called from inside an MCP server,
    the parent process's stdin is the JSON-RPC pipe to Claude Code — any
    subprocess that inherits it (or that tries to read from it) can corrupt
    the MCP framing and/or hang indefinitely. Using DEVNULL ensures git
    never pulls from that pipe.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
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


def get_dirty_diff_stat(cwd: str | Path | None = None) -> str:
    """Return ``git diff --stat HEAD`` (working-tree-vs-HEAD summary).

    Used by the queue layer to capture *what* differs from HEAD when
    ``code_dirty=True``. The plain stat is bounded in size (one line
    per changed file plus a totals row) and is forensics-friendly: lets
    you tell "I queued from a clean commit" from "I queued from a tree
    with 12 modified files" without storing megabytes of diff text.

    Returns ``""`` when:

    - the directory is not a git repo,
    - git isn't available,
    - the tree is clean (no diff to report).

    The query covers staged + unstaged changes (``HEAD`` ⇒ working tree).
    Untracked files are *not* in this stat — they wouldn't be in a
    real diff against HEAD. If you want them, add a separate
    ``ls-files --others --exclude-standard`` capture later; the stat
    alone is the 80% case for "what changed since the recorded commit."
    """
    directory = Path(cwd) if cwd is not None else Path.cwd()
    return _run_git(["diff", "--stat", "HEAD"], directory)


def get_dirty_diff_summary(cwd: str | Path | None = None) -> dict[str, Any]:
    """Return a structured summary of working-tree state vs HEAD.

    Includes the diff stat (for human review) and a count of untracked
    files (for "did I forget to ``git add``?" forensics). All values
    are strings or ints; safe to drop directly into ``job.doc``.
    """
    directory = Path(cwd) if cwd is not None else Path.cwd()
    stat = _run_git(["diff", "--stat", "HEAD"], directory)
    porcelain = _run_git(["status", "--porcelain"], directory)
    # Count modified vs untracked. Lines starting with `??` are untracked;
    # everything else is some flavor of staged/unstaged change.
    modified = 0
    untracked = 0
    for line in porcelain.splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.strip():
            modified += 1
    return {
        "diff_stat": stat,
        "modified_count": modified,
        "untracked_count": untracked,
    }
