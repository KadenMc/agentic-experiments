"""Tests for the utility helpers (atomic, git, paths)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aexp.utils.atomic import atomic_write
from aexp.utils.git import get_git_provenance
from aexp.utils.paths import (
    INSTALLED_MARKER_REL,
    RepoRootNotFound,
    find_repo_root,
    read_installed_marker,
    resolve_invocation,
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
    (repo / ".aexp").mkdir(parents=True)
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
    # Cross-platform invocation fields captured by default.
    assert "python_exe" in marker
    assert Path(marker["python_exe"]).exists()
    assert "conda_env_name" in marker  # may be "" if install was from a venv


def test_write_installed_marker_honors_explicit_python_exe(tmp_path: Path) -> None:
    write_installed_marker(
        tmp_path,
        version="0.1.0",
        run_store_path=".runs",
        limina_vendor_sha="x",
        python_exe="/custom/python",
        conda_env_name="custom-env",
    )
    marker = read_installed_marker(tmp_path)
    assert marker is not None
    assert marker["python_exe"] == "/custom/python"
    assert marker["conda_env_name"] == "custom-env"


def test_resolve_invocation_uses_conda_run_when_env_name_present(tmp_path: Path) -> None:
    write_installed_marker(
        tmp_path,
        version="0.1.0",
        run_store_path=".runs",
        limina_vendor_sha="x",
        python_exe="/opt/miniforge3/envs/agentic-exp/bin/python",
        conda_env_name="agentic-exp",
    )
    argv = resolve_invocation(tmp_path)
    assert argv[:4] == ["conda", "run", "-n", "agentic-exp"]
    assert argv[-3:] == ["python", "-m", "aexp"]


def test_resolve_invocation_falls_back_to_python_exe_when_venv(tmp_path: Path) -> None:
    write_installed_marker(
        tmp_path,
        version="0.1.0",
        run_store_path=".runs",
        limina_vendor_sha="x",
        python_exe="/home/u/.venv/bin/python",
        conda_env_name="",  # venv, not conda
    )
    argv = resolve_invocation(tmp_path)
    assert argv == ["/home/u/.venv/bin/python", "-m", "aexp"]


def test_resolve_invocation_last_resort_without_marker(tmp_path: Path) -> None:
    """No marker on disk → fall back to sys.executable."""
    import sys

    argv = resolve_invocation(tmp_path)
    assert argv == [sys.executable, "-m", "aexp"]


def test_conda_env_detection_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even without CONDA_DEFAULT_ENV, the env name can be inferred from the path."""
    from aexp.utils.paths import _detect_conda_env_name

    # Clear any inherited env vars so we exercise the path-parsing branch.
    monkeypatch.delenv("CONDA_DEFAULT_ENV", raising=False)
    assert _detect_conda_env_name(
        "C:\\Users\\me\\miniforge3\\envs\\agentic-exp\\python.exe"
    ) == "agentic-exp"
    assert _detect_conda_env_name("/home/me/mambaforge/envs/myenv/bin/python") == "myenv"
    # No envs/ segment -> empty string.
    assert _detect_conda_env_name("/usr/bin/python3") == ""


def test_conda_env_detection_prefers_env_var_over_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If CONDA_DEFAULT_ENV is set and is not 'base', it wins."""
    from aexp.utils.paths import _detect_conda_env_name

    monkeypatch.setenv("CONDA_DEFAULT_ENV", "explicit-env")
    # Path says 'other-env' but the env var wins.
    assert _detect_conda_env_name(
        "/home/me/miniforge3/envs/other-env/bin/python"
    ) == "explicit-env"


def test_conda_env_detection_falls_back_on_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONDA_DEFAULT_ENV='base' doesn't count as a useful env name — parse the path."""
    from aexp.utils.paths import _detect_conda_env_name

    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    assert _detect_conda_env_name(
        "/home/me/miniforge3/envs/agentic-exp/bin/python"
    ) == "agentic-exp"


def test_read_installed_marker_missing_returns_none(tmp_path: Path) -> None:
    assert read_installed_marker(tmp_path) is None


def test_read_installed_marker_invalid_json_returns_none(tmp_path: Path) -> None:
    (tmp_path / ".aexp").mkdir()
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
