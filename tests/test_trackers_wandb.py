"""Tests for the W&B tracker adapter.

Skipped entirely if ``wandb`` isn't installed. Otherwise runs against a
monkeypatched ``wandb`` module so no network calls go out.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

wandb = pytest.importorskip("wandb")

from aexp.install import install_limina  # noqa: E402
from aexp.runs import create_run  # noqa: E402
from aexp.trackers import (  # noqa: E402
    TrackerInitError,
    bind_tracker,
)

# ---------------------------------------------------------------------------
# Fake wandb module
# ---------------------------------------------------------------------------


class _FakeRun:
    def __init__(self, project: str, group: str, tags: list[str], config: dict, notes: str | None):
        self.id = "fake-run-42"
        self.url = f"https://wandb.test/{project}/{self.id}"
        self.project = project
        self.group = group
        self.tags = tags
        self.config = dict(config)
        self.notes = notes
        self.state = "running"
        self.logged: list[dict] = []
        self.artifacts: list[tuple[str, str]] = []
        self.finished_code: int | None = None

    def log(self, metrics: dict) -> None:
        self.logged.append(dict(metrics))

    def log_artifact(self, artifact: object) -> None:
        self.artifacts.append(("log_artifact", repr(artifact)))

    def finish(self, exit_code: int = 0) -> None:
        self.state = "finished"
        self.finished_code = exit_code


class _FakeArtifact:
    def __init__(self, name: str, type: str) -> None:
        self.name = name
        self.type = type
        self.files: list[str] = []

    def add_file(self, path: str) -> None:
        self.files.append(path)


@pytest.fixture
def fake_wandb(monkeypatch: pytest.MonkeyPatch) -> object:
    """Replace the imported ``wandb`` module with a stub that records calls."""
    from aexp.trackers import wandb_adapter as wa

    class _FakeModule:
        last_init_kwargs: dict = {}
        last_run: _FakeRun | None = None

        @staticmethod
        def init(**kwargs):
            _FakeModule.last_init_kwargs = dict(kwargs)
            run = _FakeRun(
                project=kwargs["project"],
                group=kwargs["group"],
                tags=kwargs.get("tags", []),
                config=kwargs.get("config", {}),
                notes=kwargs.get("notes"),
            )
            _FakeModule.last_run = run
            return run

        Artifact = _FakeArtifact

        @staticmethod
        def log(metrics: dict) -> None:
            if _FakeModule.last_run:
                _FakeModule.last_run.log(metrics)

        @staticmethod
        def finish(exit_code: int = 0) -> None:
            if _FakeModule.last_run:
                _FakeModule.last_run.finish(exit_code)

        @staticmethod
        def log_artifact(artifact: object) -> None:
            if _FakeModule.last_run:
                _FakeModule.last_run.log_artifact(artifact)

        class Api:
            def runs(self, path: str, filters: dict | None = None):  # noqa: D401
                return []

    # Patch both the actual sys.modules entry and the cached import in wa.
    monkeypatch.setitem(sys.modules, "wandb", _FakeModule)
    monkeypatch.setattr(wa, "WandbAdapter", wa.WandbAdapter)  # no-op but forces fresh lookup
    return _FakeModule


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
# Tests
# ---------------------------------------------------------------------------


def test_wandb_adapter_init_passes_core_kwargs(fake_wandb, installed_repo: Path) -> None:
    from aexp.trackers import WandbAdapter

    adapter = WandbAdapter()
    job = create_run(
        experiment_id="E018",
        hypothesis_id="H012",
        statepoint={"condition": "full", "model": "smoke"},
        repo_root=installed_repo,
    )
    handle = bind_tracker(job, adapter, project="proj-x", repo_root=installed_repo)
    assert handle.url == "https://wandb.test/proj-x/fake-run-42"
    assert handle.group == "H012/E018/full"
    kw = fake_wandb.last_init_kwargs
    assert kw["project"] == "proj-x"
    assert kw["group"] == "H012/E018/full"
    assert "E018" in kw["tags"] and "H012" in kw["tags"]
    # Config carries the full run-link chain + sp
    assert kw["config"]["aexp"]["experiment_id"] == "E018"
    assert kw["config"]["condition"] == "full"


def test_wandb_adapter_co_locates_dir_with_job_workspace(
    fake_wandb, installed_repo: Path
) -> None:
    """``bind_tracker`` must thread the signac workspace into ``wandb.init(dir=...)``.

    Guarantees the HPC workflow: offline-run dirs live at
    ``<repo>/.runs/workspace/<job_id>/wandb/offline-run-*/``.
    """
    from aexp.trackers import WandbAdapter

    adapter = WandbAdapter()
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    bind_tracker(job, adapter, project="p", repo_root=installed_repo)
    kw = fake_wandb.last_init_kwargs
    assert "dir" in kw, kw.keys()
    # The dir must resolve to the signac job's workspace path.
    assert Path(kw["dir"]).resolve() == Path(job.path).resolve()


def test_wandb_adapter_offline_mode(fake_wandb, installed_repo: Path) -> None:
    from aexp.trackers import WandbAdapter

    adapter = WandbAdapter()
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    bind_tracker(job, adapter, project="p", offline=True, repo_root=installed_repo)
    assert fake_wandb.last_init_kwargs["mode"] == "offline"


def test_wandb_adapter_log_finish_roundtrip(fake_wandb, installed_repo: Path) -> None:
    from aexp.trackers import WandbAdapter

    adapter = WandbAdapter()
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    handle = bind_tracker(job, adapter, project="p", repo_root=installed_repo)
    adapter.log(handle, {"loss": 0.3})
    adapter.finish(handle, exit_code=0)
    run = fake_wandb.last_run
    assert run.logged == [{"loss": 0.3}]
    assert run.state == "finished"
    assert run.finished_code == 0


def test_find_offline_runs_walks_workspaces(
    installed_repo: Path,
) -> None:
    """``find_offline_runs`` discovers any ``offline-run-*`` under the run store."""
    from aexp.trackers import find_offline_runs

    workspace = installed_repo / ".runs" / "workspace"
    # Two fake offline runs under two different jobs + one bare-run-store dir.
    for jid, rid in [
        ("jobA", "offline-run-20260421_100000-aaaa"),
        ("jobB", "offline-run-20260421_110000-bbbb"),
    ]:
        d = workspace / jid / "wandb" / rid
        d.mkdir(parents=True)
        (d / "wandb-summary.json").write_text("{}", encoding="utf-8")
    # Also plant one at a legacy flat location to ensure rglob still finds it.
    legacy = installed_repo / ".runs" / "wandb" / "offline-run-legacy"
    legacy.mkdir(parents=True)

    found = find_offline_runs(installed_repo / ".runs")
    assert len(found) == 3
    names = {p.name for p in found}
    assert names == {
        "offline-run-20260421_100000-aaaa",
        "offline-run-20260421_110000-bbbb",
        "offline-run-legacy",
    }


def test_sync_offline_runs_dry_run_is_noop(installed_repo: Path) -> None:
    """``sync_offline_runs(dry_run=True)`` reports paths without invoking wandb."""
    from aexp.trackers import sync_offline_runs

    workspace = installed_repo / ".runs" / "workspace"
    d = workspace / "jobA" / "wandb" / "offline-run-dry-run-marker"
    d.mkdir(parents=True)

    results = sync_offline_runs(installed_repo / ".runs", dry_run=True)
    assert len(results) == 1
    assert results[0].ok is True
    assert "dry-run" in results[0].stdout


def test_sync_offline_runs_empty_store_returns_empty_list(installed_repo: Path) -> None:
    from aexp.trackers import sync_offline_runs

    assert sync_offline_runs(installed_repo / ".runs") == []


def test_sync_offline_runs_handles_wandb_invocation_failure(
    installed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``python -m wandb sync`` fails, the error is captured on the result, not raised."""
    from aexp.trackers import sync_offline_runs

    workspace = installed_repo / ".runs" / "workspace"
    d = workspace / "jobA" / "wandb" / "offline-run-bogus"
    d.mkdir(parents=True)

    def _fail_run(*args, **kwargs):
        import subprocess

        raise subprocess.TimeoutExpired(cmd=args[0] if args else "wandb", timeout=1)

    monkeypatch.setattr("aexp.trackers.wandb_adapter.subprocess.run", _fail_run)
    results = sync_offline_runs(installed_repo / ".runs")
    assert len(results) == 1
    assert not results[0].ok
    assert results[0].returncode == 124
    assert "timeout" in results[0].stderr


def test_wandb_adapter_raises_on_missing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``import wandb`` fails, constructing WandbAdapter should raise TrackerInitError."""
    # Force an ImportError by removing 'wandb' and any cached adapter module state.
    monkeypatch.setitem(sys.modules, "wandb", None)
    # Also drop our adapter's cached reference so it has to re-import.
    import aexp.trackers.wandb_adapter as wa

    # Re-exec the adapter's classmethod directly: patch _import_wandb's namespace.
    with pytest.raises(TrackerInitError, match="wandb is not installed"):
        wa.WandbAdapter()
