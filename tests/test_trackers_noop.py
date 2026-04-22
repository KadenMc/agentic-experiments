"""Tests for the NoopAdapter + bind_tracker wiring against NoopAdapter."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aexp.install import install_limina
from aexp.runs import create_run
from aexp.trackers import NoopAdapter, RunHandle, bind_tracker


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
    install_limina(repo)
    return repo


# ---------------------------------------------------------------------------
# NoopAdapter in isolation (log_root mode)
# ---------------------------------------------------------------------------


def test_noop_adapter_writes_jsonl(tmp_path: Path) -> None:
    adapter = NoopAdapter(log_root=tmp_path)
    handle = adapter.init_run(
        project="p",
        group="g",
        tags=["t1"],
        config={"k": "v", "job_id": "x"},
        notes="note",
    )
    assert handle.backend == "noop"
    adapter.log(handle, {"loss": 0.1})
    adapter.log(handle, {"loss": 0.05, "acc": 0.9})
    adapter.finish(handle, exit_code=0)

    events_path = Path(handle.extra["log_dir"]) / "events.jsonl"
    assert events_path.is_file()
    lines = events_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    kinds = [r["event"] for r in records]
    assert kinds == ["init_run", "log", "log", "finish"]


def test_noop_adapter_list_runs_roundtrip(tmp_path: Path) -> None:
    adapter = NoopAdapter(log_root=tmp_path)
    h1 = adapter.init_run(project="p", group="g1/x", tags=[], config={"job_id": "a"})
    adapter.log(h1, {"loss": 1.0})
    adapter.finish(h1, exit_code=0)
    h2 = adapter.init_run(project="p", group="g2/y", tags=[], config={"job_id": "b"})
    adapter.log(h2, {"loss": 0.5})  # no finish → running

    all_p = adapter.list_runs(project="p", group_prefix="")
    ids = {r.id for r in all_p}
    assert {h1.id, h2.id}.issubset(ids)

    g1 = adapter.list_runs(project="p", group_prefix="g1/")
    assert [r.id for r in g1] == [h1.id]
    assert g1[0].state == "complete"


def test_noop_adapter_list_runs_empty_without_log_root() -> None:
    adapter = NoopAdapter()
    assert adapter.list_runs(project="p", group_prefix="") == []


def test_noop_adapter_log_artifact_records_size(tmp_path: Path) -> None:
    adapter = NoopAdapter(log_root=tmp_path)
    handle = adapter.init_run(project="p", group="g", tags=[], config={"job_id": "x"})
    fpath = tmp_path / "out.txt"
    fpath.write_text("hello", encoding="utf-8")
    adapter.log_artifact(handle, "out", fpath)
    events = Path(handle.extra["log_dir"]) / "events.jsonl"
    records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    art = [r for r in records if r["event"] == "log_artifact"]
    assert len(art) == 1
    assert art[0]["size_bytes"] == 5


# ---------------------------------------------------------------------------
# bind_tracker wiring
# ---------------------------------------------------------------------------


def test_bind_tracker_writes_job_doc(installed_repo: Path, tmp_path: Path) -> None:
    job = create_run(
        experiment_id="E018",
        hypothesis_id="H012",
        statepoint={"condition": "full", "model": "smoke"},
        repo_root=installed_repo,
    )
    adapter = NoopAdapter(log_root=tmp_path / "noop_tracker")
    handle = bind_tracker(
        job,
        adapter,
        project="unit-test-project",
        repo_root=installed_repo,
    )
    assert isinstance(handle, RunHandle)
    tracker_doc = job.doc["tracker"]
    assert tracker_doc["backend"] == "noop"
    assert tracker_doc["project"] == "unit-test-project"
    assert tracker_doc["group"] == "H012/E018/full"
    assert tracker_doc["run_id"] == handle.id


def test_bind_tracker_uses_deterministic_group(installed_repo: Path, tmp_path: Path) -> None:
    job = create_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"condition": "classify"},
        repo_root=installed_repo,
    )
    adapter = NoopAdapter(log_root=tmp_path / "noop_tracker")
    handle = bind_tracker(
        job,
        adapter,
        project="p",
        repo_root=installed_repo,
    )
    assert handle.group == "H001/E001/classify"


def test_bind_tracker_extra_tags_appended(installed_repo: Path, tmp_path: Path) -> None:
    job = create_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    adapter = NoopAdapter(log_root=tmp_path / "noop_tracker")
    bind_tracker(
        job,
        adapter,
        project="p",
        extra_tags=["rerun"],
        repo_root=installed_repo,
    )
    # Read the init_run event from the JSONL log to confirm tags were forwarded.
    events = list((tmp_path / "noop_tracker").rglob("events.jsonl"))
    assert events, "no events.jsonl written"
    init = [
        json.loads(line)
        for line in events[0].read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "init_run"
    ][0]
    assert "rerun" in init["tags"]
    assert "kind=experiment" in init["tags"]
    assert "H001" in init["tags"]
    assert "E001" in init["tags"]
    assert "condition=full" in init["tags"]
