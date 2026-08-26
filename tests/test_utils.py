"""Tests for the utility helpers (atomic, git, paths)."""
from __future__ import annotations

import json
import multiprocessing as mp
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


def test_atomic_write_cleans_up_tmp_when_the_write_fails(tmp_path: Path) -> None:
    """A failed write leaves no orphan.

    The temp name carries a nonce, so it is no longer self-clobbering: the old shared
    name was simply overwritten by the next attempt, whereas a unique name would
    accumulate one stranded file per failure without this cleanup.

    The failure used here is a real one this codebase already cares about -- content the
    target encoding cannot represent, the same class as the cp1252 problem that made
    ``encoding="utf-8"`` explicit here in the first place. ``open`` creates the temp
    before ``write`` raises, so there genuinely is an orphan to clean up.
    """
    target = tmp_path / "y.txt"
    with pytest.raises(UnicodeEncodeError):
        atomic_write(target, "lone surrogate: \udc80", encoding="utf-8")
    assert list(tmp_path.iterdir()) == [], "a failed write stranded a temp file"


# -- concurrent writers -------------------------------------------------------------
# `replace` being atomic only makes the *publish* indivisible. It says nothing about who
# filled the file being published, so a temp path derived from the destination alone lets
# the interleaving move upstream from `dest` to `dest.tmp`. Pre-nonce these two tests
# failed: on Linux with a spliced file (measured 1.3-98% of destinations across xfs/NFS,
# 2-4 writers and three flush shapes), on Windows with PermissionError on the concurrent
# open. The rate is load- and shape-dependent, which is why the behavioural test below
# repeats trials and why the name test beside it fails deterministically instead.

_SIZES = {"A": 200_000, "B": 40_000}


def _payload(tag: str) -> str:
    return json.dumps({"writer": tag, "data": tag * _SIZES[tag]})


def _concurrent_writer(args: tuple[str, str]) -> None:
    """Spawn entry point: one writer, one write, into a destination it shares."""
    dest, tag = args
    from aexp.utils.atomic import atomic_write as _aw

    _aw(Path(dest), _payload(tag))


def test_atomic_write_is_safe_under_concurrent_writers(tmp_path: Path) -> None:
    """Two processes writing one destination publish one writer's payload, whole.

    This is the property `aexp.workpool.WorkPool` rests its termination proof on: the
    pool permits occasional double-processing of an item, so two workers can end up
    writing one output path, and `is_done` going true over a spliced file would be
    monotone and irreversible. Which writer wins is unspecified; that exactly one does
    is not.

    Payloads are large (many write() syscalls) and of different lengths, so a splice is
    detectable rather than coincidentally valid. Trials repeat because the race is
    timing-dependent -- one pass is a weak probe of a few-percent event.
    """
    ctx = mp.get_context("spawn")
    expected = {_payload(t).encode() for t in _SIZES}
    torn: list[str] = []

    for trial in range(12):
        dest = tmp_path / f"item_{trial}.json"
        procs = [ctx.Process(target=_concurrent_writer, args=((str(dest), t),))
                 for t in _SIZES]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join(timeout=60)

        codes = [pr.exitcode for pr in procs]
        for pr in procs:
            if pr.is_alive():
                pr.terminate()
        assert codes == [0] * len(procs), (
            f"trial {trial}: writer exit codes {codes} -- a non-zero code means the write "
            "itself raised (pre-nonce: FileNotFoundError on POSIX once the peer renamed "
            "the shared temp away, PermissionError on Windows on the concurrent open)"
        )

        assert dest.exists(), f"trial {trial}: neither writer published anything"
        raw = dest.read_bytes()
        if raw not in expected:
            torn.append(f"trial {trial}: {len(raw)}B, expected one of "
                        f"{sorted(len(e) for e in expected)}")

    assert not torn, "published file was a splice of both writers: " + "; ".join(torn)


def test_atomic_write_temp_name_is_unique_per_call(tmp_path: Path) -> None:
    """The nonce is what makes concurrent writers independent -- pin it directly.

    The behavioral test above can only fail probabilistically. This one fails
    deterministically the moment the temp path goes back to being derived from the
    destination alone.
    """
    seen: list[str] = []
    target = tmp_path / "z.txt"
    real_replace = Path.replace

    def _capture(self: Path, other):  # noqa: ANN001, ANN202
        seen.append(self.name)
        return real_replace(self, other)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(Path, "replace", _capture)
        for _ in range(5):
            atomic_write(target, "ok")

    assert len(set(seen)) == len(seen), f"temp name was reused across calls: {seen}"
    assert all(n.startswith("z.txt.") and n.endswith(".tmp") for n in seen), seen


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
        scaffold_sha="deadbeef",
    )
    marker = read_installed_marker(tmp_path)
    assert marker is not None
    assert marker["version"] == "0.1.0"
    assert marker["run_store_path"] == ".runs"
    assert marker["scaffold_sha"] == "deadbeef"
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
        scaffold_sha="x",
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
        scaffold_sha="x",
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
        scaffold_sha="x",
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
        scaffold_sha="x",
    )
    assert resolve_run_store_path(tmp_path) == (tmp_path / "custom" / "runs").resolve()


def test_resolve_run_store_path_defaults(tmp_path: Path) -> None:
    # No marker -> default .runs
    assert resolve_run_store_path(tmp_path) == (tmp_path / ".runs").resolve()


def test_installed_marker_is_valid_json_with_trailing_newline(tmp_path: Path) -> None:
    write_installed_marker(
        tmp_path, version="0.1.0", run_store_path=".runs", scaffold_sha="x"
    )
    raw = (tmp_path / INSTALLED_MARKER_REL).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw)["version"] == "0.1.0"
