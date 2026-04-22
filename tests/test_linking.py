"""Tests for batch queries + retroactive linking."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from aexp.install import install_limina
from aexp.linking import (
    link_to_experiment,
    list_batches,
    runs_for_experiment,
    show_batch,
    summarize_run,
)
from aexp.runs import create_run, mark_status


def _git_commit(repo: Path, msg: str = "seed") -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture_output=True
    )
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", msg],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def installed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_commit(repo)
    install_limina(repo)
    return repo


# ---------------------------------------------------------------------------
# runs_for_experiment
# ---------------------------------------------------------------------------


def test_runs_for_experiment_filters(installed_repo: Path) -> None:
    create_run(experiment_id="E001", statepoint={"c": "a"}, repo_root=installed_repo)
    create_run(experiment_id="E002", statepoint={"c": "b"}, repo_root=installed_repo)
    e001 = runs_for_experiment("E001", repo_root=installed_repo)
    assert len(e001) == 1
    assert e001[0].sp["c"] == "a"


# ---------------------------------------------------------------------------
# summarize_run
# ---------------------------------------------------------------------------


def test_summarize_run_flattens_metadata(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E018",
        hypothesis_id="H012",
        statepoint={"condition": "full", "seed": 0},
        repo_root=installed_repo,
    )
    s = summarize_run(job)
    assert s.job_id == job.id
    assert s.experiment_id == "E018"
    assert s.hypothesis_id == "H012"
    assert s.batch_slug == "H012/E018/full"
    assert s.sp["seed"] == 0
    assert s.status == "created"


def test_summarize_run_slug_falls_back_to_short_id(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E018",
        statepoint={"no_condition": True},
        repo_root=installed_repo,
    )
    s = summarize_run(job)
    # No condition in sp and no hypothesis → batch_slug uses short id fallback.
    assert s.batch_slug is not None
    assert s.batch_slug.startswith("_/E018/")
    assert len(s.batch_slug.split("/")[-1]) == 8


# ---------------------------------------------------------------------------
# list_batches
# ---------------------------------------------------------------------------


def test_list_batches_groups_by_condition(installed_repo: Path) -> None:
    # Distinct seeds so each call yields a distinct signac job.
    for i, cond in enumerate(("full", "full", "full", "classify", "classify")):
        create_run(
            experiment_id="E018",
            hypothesis_id="H012",
            statepoint={"condition": cond, "seed": i},
            repo_root=installed_repo,
        )
    batches = list_batches(experiment_id="E018", repo_root=installed_repo)
    assert {b.selector["condition"] for b in batches} == {"full", "classify"}
    counts = {b.selector["condition"]: b.count for b in batches}
    assert counts == {"full": 3, "classify": 2}


def test_list_batches_rolls_up_status_counts(installed_repo: Path) -> None:
    create_run(
        experiment_id="E001", statepoint={"condition": "a", "seed": 0}, repo_root=installed_repo
    )
    j2 = create_run(
        experiment_id="E001", statepoint={"condition": "a", "seed": 1}, repo_root=installed_repo
    )
    j3 = create_run(
        experiment_id="E001", statepoint={"condition": "a", "seed": 2}, repo_root=installed_repo
    )
    mark_status(j2, "complete")
    mark_status(j3, "failed")
    batches = list_batches(experiment_id="E001", repo_root=installed_repo)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.status_counts.get("created") == 1
    assert batch.status_counts.get("complete") == 1
    assert batch.status_counts.get("failed") == 1


def test_list_batches_custom_selector_keys(installed_repo: Path) -> None:
    for cond, model in (("full", "a"), ("full", "b"), ("classify", "a")):
        create_run(
            experiment_id="E001",
            statepoint={"condition": cond, "model": model},
            repo_root=installed_repo,
        )
    batches = list_batches(
        experiment_id="E001",
        selector_keys=("condition", "model"),
        repo_root=installed_repo,
    )
    # Three distinct (condition, model) slices → three batches of 1 each.
    assert len(batches) == 3
    assert all(b.count == 1 for b in batches)


# ---------------------------------------------------------------------------
# show_batch
# ---------------------------------------------------------------------------


def test_show_batch_exact_match(installed_repo: Path) -> None:
    create_run(
        experiment_id="E001", statepoint={"condition": "full"}, repo_root=installed_repo
    )
    create_run(
        experiment_id="E001", statepoint={"condition": "classify"}, repo_root=installed_repo
    )
    rows = show_batch(
        experiment_id="E001",
        selector={"condition": "full"},
        repo_root=installed_repo,
    )
    assert len(rows) == 1
    assert rows[0].sp["condition"] == "full"


# ---------------------------------------------------------------------------
# link_to_experiment
# ---------------------------------------------------------------------------


def test_link_to_experiment_overwrites_doc(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001", statepoint={"c": "f"}, repo_root=installed_repo
    )
    # Retroactively repoint the link.
    link_to_experiment(
        job.id,
        experiment_id="E099",
        hypothesis_id="H099",
        experiment_path="kb/research/experiments/E099-repointed.md",
        repo_root=installed_repo,
    )
    from aexp.runs import open_run
    reopened = open_run(job.id, repo_root=installed_repo)
    assert reopened.doc["limina"]["experiment_id"] == "E099"
    assert reopened.doc["limina"]["hypothesis_id"] == "H099"
    assert reopened.doc["limina"]["experiment_path"].endswith("E099-repointed.md")


def test_link_to_experiment_rejects_bad_id(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001", statepoint={"c": "f"}, repo_root=installed_repo
    )
    with pytest.raises(ValidationError):
        link_to_experiment(job.id, experiment_id="bad-id", repo_root=installed_repo)
