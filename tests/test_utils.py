"""Tests for the utility helpers (atomic, git, paths)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_experiments.utils.atomic import atomic_write
from agentic_experiments.utils.git import get_git_provenance
from agentic_experiments.utils.paths import (
    INSTALLED_MARKER_REL,
    RepoRootNotFound,
    find_repo_root,
    read_installed_marker,
    resolve_run_store_path,
    write_installed_marker,
)


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------


def test_atomic_write_creates_missing_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    target = tmp_path / "c.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_forces_lf_newlines(tmp_path: Path) -> None:
    target = tmp_path / "d.txt"
    atomic_write(target, "line1\nline2\n")
    # Read raw bytes: should NOT contain CR even on Windows.
    raw = target.read_bytes()
    assert b"\r" not in raw, raw


def test_atomic_write_bytes_bypasses_encoding(tmp_path: Path) -> None:
    target = tmp_path / "bin.dat"
    atomic_write(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"


def test_atomic_write_leaves_no_tmp_on_success(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    atomic_write(target, "ok")
    siblings = {p.name for p in tmp_path.iterdir()}
    assert siblings == {"x.txt"}


# ---------------------------------------------------------------------------
# git provenance
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def fresh_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hi", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_git_provenance_clean(fresh_repo: Path) -> None:
    prov = get_git_provenance(fresh_repo)
    assert prov["commit"]
    assert len(prov["commit"]) == 40  # full SHA
    assert prov["dirty"] is False
    assert prov["branch"] == "main"


def test_git_provenance_dirty(fresh_repo: Path) -> None:
    (fresh_repo / "README.md").write_text("changed", encoding="utf-8")
    prov = get_git_provenance(fresh_repo)
    assert prov["dirty"] is True


def test_git_provenance_outside_repo_returns_empty(tmp_path: Path) -> None:
    # tmp_path is guaranteed not to be a git repo.
    prov = get_git_provenance(tmp_path)
    assert prov["commit"] == ""
    assert prov["dirty"] is False
    # branch is "" either because we're outside a repo or because rev-parse
    # --abbrev-ref returns "HEAD" in detached contexts (we normalize to "").
    assert prov["branch"] == ""


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def test_find_repo_root_detects_git_dir(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / ".git").mkdir()  # bare marker is enough; no need for a real repo
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested).resolve() == repo.resolve()


def test_find_repo_root_detects_marker(tmp_path: Path) -> None:
    repo = tmp_path / "marked"
    (repo / ".agentic_experiments").mkdir(parents=True)
    (repo / INSTALLED_MARKER_REL).write_text("{}", encoding="utf-8")
    assert find_repo_root(repo).resolve() == repo.resolve()


def test_find_repo_root_raises_when_missing(tmp_path: Path) -> None:
    subdir = tmp_path / "nowhere"
    subdir.mkdir()
    with pytest.raises(RepoRootNotFound):
        find_repo_root(subdir)


def test_write_then_read_installed_marker(tmp_path: Path) -> None:
    write_installed_marker(
        tmp_path,
        version="0.1.0",
        run_store_path=".runs",
        limina_vendor_sha="deadbeef",
    )
    marker = read_installed_marker(tmp_path)
    assert marker is not None
    assert marker["version"] == "0.1.0"
    assert marker["run_store_path"] == ".runs"
    assert marker["limina_vendor_sha"] == "deadbeef"
    assert "installed_at" in marker


def test_read_installed_marker_missing_returns_none(tmp_path: Path) -> None:
    assert read_installed_marker(tmp_path) is None


def test_read_installed_marker_invalid_json_returns_none(tmp_path: Path) -> None:
    (tmp_path / ".agentic_experiments").mkdir()
    (tmp_path / INSTALLED_MARKER_REL).write_text("not json", encoding="utf-8")
    assert read_installed_marker(tmp_path) is None


def test_resolve_run_store_path_uses_marker(tmp_path: Path) -> None:
    write_installed_marker(
        tmp_path,
        version="0.1.0",
        run_store_path="custom/runs",
        limina_vendor_sha="x",
    )
    assert resolve_run_store_path(tmp_path) == (tmp_path / "custom" / "runs").resolve()


def test_resolve_run_store_path_defaults(tmp_path: Path) -> None:
    # No marker -> default .runs
    assert resolve_run_store_path(tmp_path) == (tmp_path / ".runs").resolve()


def test_installed_marker_is_valid_json_with_trailing_newline(tmp_path: Path) -> None:
    write_installed_marker(
        tmp_path, version="0.1.0", run_store_path=".runs", limina_vendor_sha="x"
    )
    raw = (tmp_path / INSTALLED_MARKER_REL).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw)["version"] == "0.1.0"
