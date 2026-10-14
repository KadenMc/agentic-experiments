"""Hooks check the checkout an event concerns, not their working directory.

Claude Code runs a hook in the session's current directory, which can be a
subfolder or a worktree. In a desktop-app worktree session the hook runs in
the main checkout and the worktree arrives only as the input's ``cwd``.
``CLAUDE_PROJECT_DIR`` names where the session started. These tests build
real repositories and worktrees and drive each hook with those three inputs:
the process directory, the input ``cwd``, and ``CLAUDE_PROJECT_DIR``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aexp.hooks._project import checkout_for, read_event, session_checkout

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GIT_IDENTITY = ["-c", "user.email=t@e.com", "-c", "user.name=T"]
BROKEN_EXPERIMENT = Path("kb") / "research" / "experiments" / "E999-broken.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_IDENTITY, *args], cwd=str(repo), check=True, capture_output=True)


def _make_repo(scaffold: Path, dest: Path) -> Path:
    shutil.copytree(scaffold, dest)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", ".")
    _git(dest, "commit", "-q", "-m", "init")
    return dest


def _break_kb(root: Path) -> None:
    broken = root / BROKEN_EXPERIMENT
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("no frontmatter, no valid structure", encoding="utf-8")


def _run(
    hook: str, *, process_cwd: Path, event: dict | None, project: Path | None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    if project is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    return subprocess.run(
        [sys.executable, "-m", f"aexp.hooks.{hook}"],
        input=json.dumps(event) if event is not None else "",
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=str(process_cwd),
        env=env,
    )


def _write_event(path: Path, cwd: Path, content: str = "x") -> dict:
    return {"cwd": str(cwd), "tool_input": {"file_path": str(path), "content": content}}


@pytest.fixture
def project(scaffold_tree: Path, tmp_path: Path) -> Path:
    """The session's own repository, with a kb/ that validates."""
    return _make_repo(scaffold_tree, tmp_path / "project")


@pytest.fixture
def other_repo(scaffold_tree: Path, tmp_path: Path) -> Path:
    """A separate repository whose kb/ fails validation (no ACTIVE.md)."""
    repo = _make_repo(scaffold_tree, tmp_path / "other")
    (repo / "kb" / "ACTIVE.md").unlink()
    return repo


@pytest.fixture
def worktree(project: Path, tmp_path: Path) -> Path:
    """A linked worktree of ``project``."""
    wt = tmp_path / "project-wt"
    _git(project, "worktree", "add", "-q", "-b", "wt", str(wt))
    return wt


# ---------------------------------------------------------------------------
# the resolver, in process
# ---------------------------------------------------------------------------


def test_read_event_tolerates_empty_and_malformed_input() -> None:
    assert read_event("") == {}
    assert read_event("not json") == {}
    assert read_event("[1, 2]") == {}
    assert read_event('{"cwd": "x"}') == {"cwd": "x"}


def test_subfolder_resolves_to_its_checkout(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    sub = project / "kb" / "research"
    assert session_checkout({"cwd": str(sub)}) == project.resolve()


def test_worktree_counts_as_the_same_repository(
    project: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert session_checkout({"cwd": str(worktree)}) == worktree.resolve()
    edited = worktree / "kb" / "ACTIVE.md"
    assert checkout_for(edited, {"cwd": str(project)}) == worktree.resolve()


def test_worktree_inside_the_project_is_its_own_checkout(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code's default worktree location is inside the main checkout."""
    nested = project / ".claude" / "worktrees" / "feature"
    _git(project, "worktree", "add", "-q", "-b", "feature", str(nested))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert session_checkout({"cwd": str(nested / "kb")}) == nested.resolve()


def test_another_repository_is_not_this_sessions(
    project: Path, other_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert checkout_for(other_repo / "kb" / "ACTIVE.md", {"cwd": str(project)}) is None
    assert session_checkout({"cwd": str(other_repo)}) == project.resolve()


def test_without_project_variable_the_current_directory_stands_in(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert session_checkout({"cwd": str(project / "kb")}) == project.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert session_checkout({}) == plain.resolve()


# ---------------------------------------------------------------------------
# stop_validate
# ---------------------------------------------------------------------------


def test_stop_from_a_subfolder_still_validates(project: Path) -> None:
    """Before: a subfolder has no kb/ of its own, so validation was skipped."""
    _break_kb(project)
    sub = project / "kb" / "research"
    r = _run("stop_validate", process_cwd=sub, event={"cwd": str(sub)}, project=project)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "E999-broken" in r.stderr


def test_stop_in_another_repository_checks_the_project_instead(
    project: Path, other_repo: Path
) -> None:
    """Before: the other repository's kb/ was validated, and blocked the stop."""
    event = {"cwd": str(other_repo)}
    r = _run("stop_validate", process_cwd=other_repo, event=event, project=project)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    _break_kb(project)
    r = _run("stop_validate", process_cwd=other_repo, event=event, project=project)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "E999-broken" in r.stderr
    assert "ACTIVE.md" not in r.stderr


def test_stop_after_entering_a_worktree_validates_the_worktree(
    project: Path, worktree: Path
) -> None:
    _break_kb(worktree)
    r = _run("stop_validate", process_cwd=worktree, event={"cwd": str(worktree)}, project=project)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)


def test_stop_in_a_desktop_worktree_session_validates_the_worktree(
    project: Path, worktree: Path
) -> None:
    """The process runs in the main checkout; only the input names the worktree.

    Before: the main checkout was validated, so a broken worktree passed and a
    broken main checkout blocked a session that never touched it.
    """
    event = {"cwd": str(worktree)}
    _break_kb(worktree)
    r = _run("stop_validate", process_cwd=project, event=event, project=project)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)

    (worktree / BROKEN_EXPERIMENT).unlink()
    _break_kb(project)
    r = _run("stop_validate", process_cwd=project, event=event, project=project)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# kb_write_guard
# ---------------------------------------------------------------------------


def test_write_guard_leaves_another_repositorys_kb_alone(project: Path, other_repo: Path) -> None:
    """Before: blocked as 'not part of the validated research graph'."""
    target = other_repo / "kb" / "research" / "threads" / "README.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Notes\n", encoding="utf-8")
    r = _run(
        "kb_write_guard", process_cwd=project, event=_write_event(target, project), project=project
    )
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


def test_write_guard_validates_a_worktree_file_against_the_worktree(
    project: Path, worktree: Path
) -> None:
    """Desktop layout: process in the main checkout, file in the worktree.

    Before: every kb/ edit in the worktree was blocked as outside the graph.
    """
    valid = worktree / "kb" / "ACTIVE.md"
    r = _run("kb_write_guard", process_cwd=project, event=_write_event(valid, worktree), project=project)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)

    invalid = worktree / "kb" / "research" / "hypotheses" / "H050-bogus.md"
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text("this is not a valid kb/ artifact", encoding="utf-8")
    r = _run(
        "kb_write_guard", process_cwd=project, event=_write_event(invalid, worktree), project=project
    )
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "H050-bogus.md" in r.stderr


# ---------------------------------------------------------------------------
# enforce_hef_chain and session_start
# ---------------------------------------------------------------------------


def test_chain_check_leaves_another_repositorys_experiment_alone(
    project: Path, other_repo: Path
) -> None:
    """Before: blocked for a missing hypothesis reference in another repository."""
    target = other_repo / "kb" / "research" / "experiments" / "E001-elsewhere.md"
    event = _write_event(target, project, content="no hypothesis reference here")
    r = _run("enforce_hef_chain", process_cwd=project, event=event, project=project)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


def test_session_start_reads_the_worktrees_notes(project: Path, worktree: Path) -> None:
    active = worktree / "kb" / "ACTIVE.md"
    active.write_text(active.read_text(encoding="utf-8") + "\nworktree-only marker\n", encoding="utf-8")
    r = _run("session_start", process_cwd=project, event={"cwd": str(worktree)}, project=project)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "worktree-only marker" in r.stdout
