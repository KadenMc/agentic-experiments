"""Tests for the signac-backed run store + Limina-aware run API."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aexp.install import install_limina
from aexp.runs import (
    RunNotFound,
    RunStoreNotInitialized,
    create_run,
    find_runs,
    get_run_store,
    init_run_store,
    mark_status,
    open_run,
    run_lifecycle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def installed_repo(tmp_path: Path) -> Path:
    """A tmp dir with a git repo + Limina installed + signac initialized."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    # An initial commit so git_provenance returns a real sha rather than "".
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    install_limina(repo)
    return repo


# ---------------------------------------------------------------------------
# init / get
# ---------------------------------------------------------------------------


def test_init_run_store_is_idempotent(tmp_path: Path) -> None:
    p1 = init_run_store(tmp_path)
    p2 = init_run_store(tmp_path)
    assert Path(p1.path) == Path(p2.path)


def test_get_run_store_without_marker_raises(tmp_path: Path) -> None:
    # Bare dir with no .git, no marker.
    with pytest.raises(Exception):  # RepoRootNotFound or RunStoreNotInitialized
        get_run_store(tmp_path)


def test_get_run_store_respects_install_marker(installed_repo: Path) -> None:
    project = get_run_store(installed_repo)
    assert Path(project.path).name == ".runs"


def test_get_run_store_raises_when_project_missing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(RunStoreNotInitialized):
        get_run_store(tmp_path)


# ---------------------------------------------------------------------------
# create_run — state point population
# ---------------------------------------------------------------------------


def test_create_run_injects_experiment_id_into_sp(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"model": "smoke", "condition": "full"},
        repo_root=installed_repo,
    )
    assert job.sp["experiment_id"] == "E001"
    assert job.sp["model"] == "smoke"
    assert job.sp["condition"] == "full"


def test_create_run_adds_commit_by_default(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"model": "smoke"},
        repo_root=installed_repo,
    )
    assert "code_commit" in job.sp
    assert len(job.sp["code_commit"]) == 40
    # After install_limina, the fresh repo's working tree is dirty (we just
    # wrote kb/, scripts/, etc.) — ensure the flag is present as a bool.
    assert isinstance(job.sp["code_dirty"], bool)


def test_create_run_include_commit_false_skips_it(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"model": "smoke"},
        include_commit=False,
        repo_root=installed_repo,
    )
    assert "code_commit" not in job.sp
    assert "code_dirty" not in job.sp


def test_create_run_new_commit_yields_new_job_id(installed_repo: Path) -> None:
    """Plan: 'code.commit goes in the state point, so re-running at a new commit
    creates a new job directory; everything persists.'"""
    j1 = create_run(experiment_id="E001", statepoint={"c": "full"}, repo_root=installed_repo)
    id1 = j1.id

    # Make a new commit → same sp keys, different code_commit → new job.
    (installed_repo / "another.txt").write_text("more", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(installed_repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "next"],
        cwd=str(installed_repo),
        check=True,
        capture_output=True,
    )
    j2 = create_run(experiment_id="E001", statepoint={"c": "full"}, repo_root=installed_repo)
    assert j2.id != id1
    # Both directories exist on disk.
    assert (installed_repo / ".runs" / "workspace" / id1).is_dir()
    assert (installed_repo / ".runs" / "workspace" / j2.id).is_dir()


# ---------------------------------------------------------------------------
# create_run — doc stamping
# ---------------------------------------------------------------------------


def test_create_run_stamps_limina_link(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E018",
        hypothesis_id="H012",
        sub_hypothesis_id="H013",
        experiment_path="kb/research/experiments/E018-bar.md",
        statepoint={"c": "full"},
        repo_root=installed_repo,
    )
    link = job.doc["limina"]
    assert link["experiment_id"] == "E018"
    assert link["hypothesis_id"] == "H012"
    assert link["sub_hypothesis_id"] == "H013"
    assert link["experiment_path"].endswith("E018-bar.md")


def test_create_run_initial_status_is_created(installed_repo: Path) -> None:
    job = create_run(experiment_id="E001", statepoint={"c": "full"}, repo_root=installed_repo)
    assert job.doc["status"] == "created"
    assert "created_at" in job.doc


def test_create_run_merges_init_doc(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "full"},
        repo_root=installed_repo,
        init_doc={"tags": ["smoke"], "owner": "kaden"},
    )
    assert list(job.doc["tags"]) == ["smoke"]
    assert job.doc["owner"] == "kaden"


# ---------------------------------------------------------------------------
# open / find
# ---------------------------------------------------------------------------


def test_open_run_roundtrip(installed_repo: Path) -> None:
    job = create_run(experiment_id="E001", statepoint={"c": "full"}, repo_root=installed_repo)
    reopened = open_run(job.id, repo_root=installed_repo)
    assert reopened.id == job.id


def test_open_run_missing_raises(installed_repo: Path) -> None:
    with pytest.raises(RunNotFound):
        open_run("0" * 32, repo_root=installed_repo)


def test_find_runs_by_experiment(installed_repo: Path) -> None:
    create_run(experiment_id="E001", statepoint={"c": "full"}, repo_root=installed_repo)
    create_run(experiment_id="E001", statepoint={"c": "classify"}, repo_root=installed_repo)
    create_run(experiment_id="E002", statepoint={"c": "full"}, repo_root=installed_repo)
    e001 = find_runs(experiment_id="E001", repo_root=installed_repo)
    assert len(e001) == 2
    assert {j.sp["c"] for j in e001} == {"full", "classify"}


def test_find_runs_by_status(installed_repo: Path) -> None:
    j1 = create_run(experiment_id="E001", statepoint={"c": "full"}, repo_root=installed_repo)
    j2 = create_run(experiment_id="E001", statepoint={"c": "cls"}, repo_root=installed_repo)
    mark_status(j2, "complete")
    complete = find_runs(status="complete", repo_root=installed_repo)
    assert [j.id for j in complete] == [j2.id]
    created = find_runs(status="created", repo_root=installed_repo)
    assert [j.id for j in created] == [j1.id]


def test_find_runs_by_sp_keyword(installed_repo: Path) -> None:
    create_run(
        experiment_id="E001",
        statepoint={"c": "full", "seed": 0},
        repo_root=installed_repo,
    )
    create_run(
        experiment_id="E001",
        statepoint={"c": "full", "seed": 1},
        repo_root=installed_repo,
    )
    matches = find_runs(seed=0, repo_root=installed_repo)
    assert len(matches) == 1
    assert matches[0].sp["seed"] == 0


def test_find_runs_by_hypothesis(installed_repo: Path) -> None:
    create_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"c": "a"},
        repo_root=installed_repo,
    )
    create_run(
        experiment_id="E001",
        hypothesis_id="H002",
        statepoint={"c": "b"},
        repo_root=installed_repo,
    )
    found = find_runs(hypothesis_id="H001", repo_root=installed_repo)
    assert [j.sp["c"] for j in found] == ["a"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_run_lifecycle_clean_exit(installed_repo: Path) -> None:
    job = create_run(experiment_id="E001", statepoint={"c": "f"}, repo_root=installed_repo)
    with run_lifecycle(job):
        assert job.doc["status"] == "running"
        assert "started_at" in job.doc
    assert job.doc["status"] == "complete"
    assert "ended_at" in job.doc
    assert job.doc["wallclock_s"] >= 0


def test_run_lifecycle_exception_marks_failed(installed_repo: Path) -> None:
    job = create_run(experiment_id="E001", statepoint={"c": "f"}, repo_root=installed_repo)
    with pytest.raises(RuntimeError, match="boom"):
        with run_lifecycle(job):
            raise RuntimeError("boom")
    assert job.doc["status"] == "failed"
    assert "ended_at" in job.doc


def test_mark_status_sets_ended_at_on_terminal(installed_repo: Path) -> None:
    job = create_run(experiment_id="E001", statepoint={"c": "f"}, repo_root=installed_repo)
    mark_status(job, "abandoned")
    assert job.doc["status"] == "abandoned"
    assert "ended_at" in job.doc
