"""Focused tests for ``aexp.ledger`` — the universal cross-machine ledger.

Integration tests for the ledger-aware validator and the auto-promote hook
live in ``test_validate_cross_machine.py``. This file covers the ledger
module itself in depth: projection shape, sanitization invariants,
idempotency, backfill edge cases, and recovery paths.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aexp.install import install_scaffold
from aexp.ledger import (
    LEDGER_DIR_REL,
    SCHEMA_VERSION,
    backfill_ledger,
    ledger_path,
    list_ledger_job_ids,
    load_ledger_entry,
    project_to_ledger_entry,
    promote_to_ledger,
)
from aexp.runs import create_run, mark_status


def _git_commit(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture_output=True
    )
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def installed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_commit(repo)
    install_scaffold(repo)
    return repo


# ---------------------------------------------------------------------------
# Projection shape (golden-file-ish)
# ---------------------------------------------------------------------------


def test_projection_required_fields_present(installed_repo: Path) -> None:
    """A minimum-shape ledger entry has the required fields."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    for key in (
        "schema_version",
        "job_id",
        "statepoint",
        "status",
        "registered_machine",
        "promoted_at",
    ):
        assert key in entry, key
    assert entry["schema_version"] == SCHEMA_VERSION
    assert entry["job_id"] == job.id
    assert entry["status"] == "complete"


def test_projection_statepoint_round_trips(installed_repo: Path) -> None:
    """Statepoint values pass through unchanged into the ledger."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "alpha", "seed": 42, "ratio": 0.5},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    sp = entry["statepoint"]
    assert sp["c"] == "alpha"
    assert sp["seed"] == 42
    assert sp["ratio"] == 0.5


def test_projection_includes_run_link(installed_repo: Path) -> None:
    """When the doc has aexp.run_link, the entry preserves it."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    assert "run_link" in entry
    assert entry["run_link"]["experiment_id"] == "E001"


def test_projection_includes_wallclock_and_ended_at_when_present(
    installed_repo: Path,
) -> None:
    """Timing fields land in the entry when the doc has them."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    job.doc["wallclock_s"] = 12.34
    job.doc["ended_at"] = "2026-05-26T01:00:00Z"
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    assert entry["wallclock_s"] == 12.34
    assert entry["ended_at"] == "2026-05-26T01:00:00Z"


def test_projection_skips_missing_optional_fields(installed_repo: Path) -> None:
    """When the doc lacks wallclock/ended_at, the entry doesn't carry the keys."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    # Don't set wallclock_s or ended_at.
    # Hook fires on mark_status — but mark_status with default
    # set_ended_at=True sets ended_at via setdefault. So we need to
    # use set_ended_at=False to test the no-ended_at projection branch.
    mark_status(job, "complete", set_ended_at=False)
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    # ended_at not set, but the projection just doesn't include it
    assert "wallclock_s" not in entry


def test_projection_rejects_non_terminal_status(installed_repo: Path) -> None:
    """project_to_ledger_entry raises ValueError on non-terminal statuses."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    # 'created' is the default status from create_run
    with pytest.raises(ValueError, match="not terminal"):
        project_to_ledger_entry(job)


def test_projection_code_commit_lifted_to_top_level(installed_repo: Path) -> None:
    """code_commit/code_dirty from the statepoint get top-level convenience keys."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    # create_run stamps code_commit + code_dirty into statepoint
    assert "code_commit" in entry
    assert "code_dirty" in entry


# ---------------------------------------------------------------------------
# Sanitization: per-machine debris must NOT leak
# ---------------------------------------------------------------------------


def test_sanitization_drops_tracker_config(installed_repo: Path) -> None:
    """tracker.config (contains 'dir' with abs path) is stripped."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    job.doc["tracker"] = {
        "backend": "wandb",
        "run_id": "abc",
        "url": "https://wandb/foo",
        "config": {"dir": str(installed_repo.absolute()), "secret": "ssh"},
        "init_kwargs": {"dir": str(installed_repo.absolute())},
    }
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    assert "config" not in entry["tracker"]
    assert "init_kwargs" not in entry["tracker"]
    # And specifically: the abs path itself doesn't appear anywhere
    payload = json.dumps(entry)
    abs_str = str(installed_repo.absolute())
    assert abs_str not in payload, f"absolute path leaked into ledger: {abs_str}"


def test_sanitization_keeps_tracker_pointers(installed_repo: Path) -> None:
    """tracker pointers (backend, run_id, url, group, project) are preserved."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    job.doc["tracker"] = {
        "backend": "wandb",
        "run_id": "abc123",
        "url": "https://wandb.ai/foo/bar",
        "group": "g",
        "project": "proj",
    }
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    assert entry["tracker"] == {
        "backend": "wandb",
        "run_id": "abc123",
        "url": "https://wandb.ai/foo/bar",
        "group": "g",
        "project": "proj",
    }


def test_sanitization_unknown_doc_fields_not_leaked(installed_repo: Path) -> None:
    """Allowlist-based: an arbitrary doc field doesn't appear in the entry."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    job.doc["__test_secret"] = "should-not-leak"
    job.doc["__test_abs_path"] = str(installed_repo.absolute())
    mark_status(job, "complete")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    payload = json.dumps(entry)
    assert "should-not-leak" not in payload
    assert str(installed_repo.absolute()) not in payload


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_promote_twice_byte_identical_modulo_promoted_at(
    installed_repo: Path,
) -> None:
    """Two consecutive promotes produce entries identical modulo promoted_at."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    entry1 = load_ledger_entry(installed_repo, job.id)
    promote_to_ledger(job, repo_root=installed_repo)
    entry2 = load_ledger_entry(installed_repo, job.id)
    assert entry1 is not None and entry2 is not None
    # Drop the volatile timestamp before comparing
    e1 = {k: v for k, v in entry1.items() if k != "promoted_at"}
    e2 = {k: v for k, v in entry2.items() if k != "promoted_at"}
    assert e1 == e2


def test_promote_is_atomic(installed_repo: Path) -> None:
    """Promote uses atomic_write — no partial files on interrupt."""
    # We don't simulate the interrupt; we just verify the file exists in
    # its complete form after promotion (smoke test).
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    path = ledger_path(installed_repo, job.id)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["job_id"] == job.id


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def test_backfill_promotes_terminal_skips_non_terminal(
    installed_repo: Path,
) -> None:
    """backfill_ledger only promotes terminal-state jobs."""
    done = create_run(
        experiment_id="E001",
        statepoint={"c": "done"},
        repo_root=installed_repo,
    )
    mark_status(done, "failed")  # terminal
    pending = create_run(
        experiment_id="E001",
        statepoint={"c": "pending"},
        repo_root=installed_repo,
    )
    # pending stays at 'created' (non-terminal)
    import shutil

    shutil.rmtree(installed_repo / LEDGER_DIR_REL, ignore_errors=True)
    promoted, skipped = backfill_ledger(installed_repo)
    assert promoted == 1
    assert skipped == 0
    ids = list_ledger_job_ids(installed_repo)
    assert done.id in ids
    assert pending.id not in ids


def test_backfill_idempotency(installed_repo: Path) -> None:
    """Re-running backfill skips already-present entries."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")  # hook auto-promotes
    p1, s1 = backfill_ledger(installed_repo)  # already-present
    assert p1 == 0
    assert s1 == 1


def test_backfill_overwrite_re_promotes(installed_repo: Path) -> None:
    """backfill --overwrite re-promotes every terminal job."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    promoted, skipped = backfill_ledger(installed_repo, overwrite=True)
    assert promoted == 1
    assert skipped == 0


def test_backfill_no_run_store_returns_zero(tmp_path: Path) -> None:
    """backfill on a repo with no run store returns (0, 0) without raising."""
    repo = tmp_path / "no-store"
    repo.mkdir()
    # No install_scaffold call — no signac project. Function must
    # tolerate this without crashing.
    promoted, skipped = backfill_ledger(repo)
    assert promoted == 0
    assert skipped == 0


def test_backfill_machine_label_override(installed_repo: Path) -> None:
    """--machine-label tags every backfilled entry."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")
    import shutil

    shutil.rmtree(installed_repo / LEDGER_DIR_REL, ignore_errors=True)
    backfill_ledger(installed_repo, machine_label="cluster")
    entry = load_ledger_entry(installed_repo, job.id)
    assert entry is not None
    assert entry["registered_machine"] == "cluster"


# ---------------------------------------------------------------------------
# Defensive: corrupt files don't break the world
# ---------------------------------------------------------------------------


def test_load_ledger_entry_missing_returns_none(installed_repo: Path) -> None:
    assert load_ledger_entry(installed_repo, "a" * 32) is None


def test_load_ledger_entry_malformed_returns_none(installed_repo: Path) -> None:
    bad = installed_repo / LEDGER_DIR_REL / ("b" * 32 + ".json")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not valid json {{{", encoding="utf-8")
    assert load_ledger_entry(installed_repo, "b" * 32) is None


def test_list_ledger_job_ids_empty_when_dir_missing(tmp_path: Path) -> None:
    """No ledger dir → empty set, not an error."""
    repo = tmp_path / "no-ledger"
    repo.mkdir()
    assert list_ledger_job_ids(repo) == set()


# ---------------------------------------------------------------------------
# Hook robustness: a broken ledger module mustn't crash mark_status
# ---------------------------------------------------------------------------


def test_mark_status_succeeds_even_if_promote_fails(
    installed_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """When promote_to_ledger raises, mark_status still writes the status."""
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )

    def _boom(*args, **kwargs):  # noqa: ANN002
        raise RuntimeError("simulated promote failure")

    # Patch the symbol the runs.py hook imports lazily.
    monkeypatch.setattr("aexp.ledger.promote_to_ledger", _boom)
    mark_status(job, "complete")
    # Status was written despite hook failure
    assert job.doc.get("status") == "complete"
    # Failure message went to stderr (captured)
    captured = capsys.readouterr()
    assert "promote_to_ledger failed" in captured.err
    assert "simulated promote failure" in captured.err
