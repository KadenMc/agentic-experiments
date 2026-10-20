"""Hooks decode their payload as UTF-8, whatever their stdin pipe defaults to.

Claude Code writes a hook's JSON payload to stdin as UTF-8, with non-ASCII
characters left unescaped. On Windows a piped stdin defaults to cp1252 with
the ``surrogateescape`` error handler, so the read never fails: a non-ASCII
path or word simply arrives as mojibake. The hooks then looked for paths that
do not exist, so the write hooks skipped their checks without a word and the
session hooks fell back from a worktree to the project, and an ``Edit`` was
simulated with text that matched nothing in the file.

``PYTHONIOENCODING=cp1252:surrogateescape`` imposes that Windows default on
any platform, so these tests fail on the previous hooks under Linux as well.
Each payload is written the way Claude Code writes it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GIT_IDENTITY = ["-c", "user.email=t@e.com", "-c", "user.name=T"]
ACCENTED = "caf\u00e9"  # e-acute is in cp1252, but its two UTF-8 bytes read there as two other characters
AT_LEAST = "\u2265"  # U+2265 GREATER-THAN OR EQUAL TO
WARNING_SIGN = "\u26a0"  # its UTF-8 ends in 0xA0, which cp1252 reads as a no-break space


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_IDENTITY, *args], cwd=str(repo), check=True, capture_output=True)


def _make_repo(scaffold: Path, dest: Path) -> Path:
    shutil.copytree(scaffold, dest)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", ".")
    _git(dest, "commit", "-q", "-m", "init")
    return dest


def _run(hook: str, event: dict, project: Path) -> subprocess.CompletedProcess[bytes]:
    """Run a hook in ``project`` the way Claude Code does, under Windows' stdio defaults."""
    env = dict(
        os.environ, CLAUDE_PROJECT_DIR=str(project), PYTHONIOENCODING="cp1252:surrogateescape"
    )
    return subprocess.run(
        [sys.executable, "-m", f"aexp.hooks.{hook}"],
        input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=60,
        cwd=str(project),
        env=env,
    )


def _decoded(r: subprocess.CompletedProcess[bytes]) -> tuple[int, str, str]:
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


@pytest.fixture
def project(scaffold_tree: Path, tmp_path: Path) -> Path:
    """The session's own repository, at a plain ASCII path."""
    return _make_repo(scaffold_tree, tmp_path / "project")


@pytest.fixture
def accented_project(scaffold_tree: Path, tmp_path: Path) -> Path:
    """A repository under a non-ASCII folder, as a user's home folder can be."""
    return _make_repo(scaffold_tree, tmp_path / ACCENTED / "project")


@pytest.fixture
def accented_worktree(project: Path, tmp_path: Path) -> Path:
    """A linked worktree of ``project`` whose name is not ASCII."""
    wt = tmp_path / f"{ACCENTED}-wt"
    _git(project, "worktree", "add", "-q", "-b", "wt", str(wt))
    return wt


def test_session_start_reads_a_worktree_with_a_non_ascii_name(
    project: Path, accented_worktree: Path
) -> None:
    """Before: the mangled ``cwd`` matched no checkout, so it read the project's notes."""
    active = accented_worktree / "kb" / "ACTIVE.md"
    active.write_text(active.read_text(encoding="utf-8") + "\nworktree-only marker\n", encoding="utf-8")
    r = _run("session_start", {"cwd": str(accented_worktree)}, project)
    assert r.returncode == 0, _decoded(r)
    assert "worktree-only marker" in r.stdout.decode("utf-8")


def test_stop_validates_a_worktree_with_a_non_ascii_name(
    project: Path, accented_worktree: Path
) -> None:
    """Before: it validated the project instead, which is clean, so the turn ended."""
    broken = accented_worktree / "kb" / "research" / "experiments" / "E999-broken.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("no frontmatter, no valid structure", encoding="utf-8")
    r = _run("stop_validate", {"cwd": str(accented_worktree)}, project)
    assert r.returncode == 2, _decoded(r)
    assert "E999-broken" in r.stderr.decode("utf-8")


def test_write_guard_checks_a_file_under_a_non_ascii_path(accented_project: Path) -> None:
    """Before: the mangled path matched no checkout, so the write went unchecked.

    The file's own name is not ASCII either, so the BLOCKED message is too:
    it must reach Claude Code as UTF-8.
    """
    target = accented_project / "kb" / "research" / "hypotheses" / f"H050-{ACCENTED}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "this is not a valid kb/ artifact"
    target.write_text(content, encoding="utf-8")
    event = {
        "cwd": str(accented_project),
        "tool_input": {"file_path": str(target), "content": content},
    }
    r = _run("kb_write_guard", event, accented_project)
    assert r.returncode == 2, _decoded(r)
    assert f"H050-{ACCENTED}.md" in r.stderr.decode("utf-8")


def test_chain_check_applies_an_edit_whose_text_is_not_ascii(project: Path) -> None:
    """Before: the mangled ``old_string`` matched nothing in the file, so the check
    saw the unedited experiment, still citing its hypothesis, and let the edit through.
    """
    research = project / "kb" / "research"
    (research / "hypotheses").mkdir(parents=True, exist_ok=True)
    (research / "hypotheses" / "H001-baseline.md").write_text("# H001\n", encoding="utf-8")
    experiment = research / "experiments" / "E001-warmup.md"
    experiment.parent.mkdir(parents=True, exist_ok=True)
    reference = "> **Hypothesis**: H001\n"
    goal = f"> **Goal**: accuracy {AT_LEAST} 0.9\n"
    experiment.write_text(f"# E001: Warm-up\n\n{reference}{goal}", encoding="utf-8")
    event = {
        "cwd": str(project),
        "tool_input": {
            "file_path": str(experiment),
            "old_string": reference + goal,
            "new_string": goal,
        },
    }
    r = _run("enforce_hef_chain", event, project)
    assert r.returncode == 2, _decoded(r)
    assert "without a hypothesis reference" in r.stderr.decode("utf-8")


def test_jupyter_notice_relays_non_ascii_response_text(tmp_path: Path) -> None:
    """Before: read as cp1252, the warning sign's last byte became a no-break space,
    which ``strip()`` removed, so the notice ended in half a character.
    """
    payload = {
        "tool_name": "mcp__jupyter__connect_to_jupyter",
        "tool_input": {"jupyter_url": "http://testnode01:3618/"},
        "tool_response": {"content": [{"type": "text", "text": f"Connected {WARNING_SIGN}"}]},
    }
    r = _run("jupyter_connect_postuse", payload, tmp_path)
    assert r.returncode == 0, _decoded(r)
    assert f"(response: Connected {WARNING_SIGN})" in r.stdout.decode("utf-8", "replace")
