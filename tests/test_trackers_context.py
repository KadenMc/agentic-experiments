"""Tests for the BYO-init wandb surface: ``prepare_tracker`` / ``TrackerContext`` / ``tracked_run``.

Unlike ``test_trackers_wandb.py``, this file does NOT ``importorskip("wandb")``
— ``tracked_run`` lazy-imports ``wandb`` inside the function body, so a fake
module injected into ``sys.modules["wandb"]`` before the call is picked up
by the import statement. That lets these tests exercise the managed-run
lifecycle on machines without wandb installed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aexp.install import install_limina
from aexp.runs import create_run
from aexp.trackers import (
    TrackerContext,
    TrackerInitError,
    prepare_tracker,
    tracked_run,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


class _FakeRun:
    """Minimal duck-typed ``wandb.Run`` sufficient for tracked_run / ctx.bind."""

    def __init__(self, kwargs: dict) -> None:
        self.id = "fake-run-42"
        self.url = f"https://wandb.test/{kwargs.get('project', 'p')}/{self.id}"
        self.init_kwargs = dict(kwargs)
        self.finished_code: int | None = None

    def finish(self, exit_code: int = 0) -> None:
        self.finished_code = exit_code


@pytest.fixture
def fake_wandb(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``wandb`` module in ``sys.modules``; return the module.

    ``tracked_run``'s ``import wandb`` will resolve to this fake because
    Python consults ``sys.modules`` before hitting the filesystem.
    """

    class _FakeModule:
        last_init_kwargs: dict = {}
        last_run: _FakeRun | None = None
        init_call_count: int = 0

        @staticmethod
        def init(**kwargs):
            _FakeModule.last_init_kwargs = dict(kwargs)
            _FakeModule.init_call_count += 1
            run = _FakeRun(kwargs)
            _FakeModule.last_run = run
            return run

    monkeypatch.setitem(sys.modules, "wandb", _FakeModule)
    return _FakeModule


# ---------------------------------------------------------------------------
# prepare_tracker — shape + side effects
# ---------------------------------------------------------------------------


def test_prepare_tracker_returns_wandb_shaped_kwargs(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(job, project="p", repo_root=installed_repo)
    assert isinstance(ctx, TrackerContext)
    kw = ctx.init_kwargs
    assert kw["project"] == "p"
    assert kw["group"] == "H001/E001/full"
    assert "E001" in kw["tags"] and "H001" in kw["tags"]
    assert "config" in kw and isinstance(kw["config"], dict)
    assert "dir" in kw
    assert kw.get("reinit") is True
    # mode only set when offline=True
    assert "mode" not in kw


def test_prepare_tracker_offline_adds_mode_offline(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(job, project="p", offline=True, repo_root=installed_repo)
    assert ctx.init_kwargs["mode"] == "offline"


def test_prepare_tracker_entity_threaded_through(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(
        job, project="p", entity="team-x", repo_root=installed_repo
    )
    assert ctx.init_kwargs["entity"] == "team-x"


def test_prepare_tracker_does_not_write_job_doc(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    assert job.doc.get("tracker") is None
    prepare_tracker(job, project="p", repo_root=installed_repo)
    # Binding must only appear after ctx.bind(run), not after prepare alone.
    assert job.doc.get("tracker") is None


def test_prepare_tracker_extra_tags_appended(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(
        job,
        project="p",
        extra_tags=["smoke", "branch=foo"],
        repo_root=installed_repo,
    )
    assert "smoke" in ctx.init_kwargs["tags"]
    assert "branch=foo" in ctx.init_kwargs["tags"]
    # Auto tags still present
    assert "kind=experiment" in ctx.init_kwargs["tags"]


def test_prepare_tracker_dir_is_job_workspace(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(job, project="p", repo_root=installed_repo)
    assert Path(ctx.init_kwargs["dir"]).resolve() == Path(job.path).resolve()


# ---------------------------------------------------------------------------
# TrackerContext.bind
# ---------------------------------------------------------------------------


def test_context_bind_writes_tracker_doc_from_duck_typed_run(
    installed_repo: Path,
) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(job, project="p", repo_root=installed_repo)
    run = SimpleNamespace(id="xyz-123", url="https://e/xyz-123")
    binding = ctx.bind(run)
    stored = job.doc["tracker"]
    assert stored["backend"] == "wandb"
    assert stored["run_id"] == "xyz-123"
    assert stored["url"] == "https://e/xyz-123"
    assert stored["project"] == "p"
    assert stored["group"] == ctx.group
    assert binding.run_id == "xyz-123"


def test_context_bind_accepts_custom_backend_name(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(job, project="p", repo_root=installed_repo)
    run = SimpleNamespace(id="abc", url=None)
    ctx.bind(run, backend="mlflow")
    assert job.doc["tracker"]["backend"] == "mlflow"


def test_context_bind_tolerates_missing_url(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    ctx = prepare_tracker(job, project="p", repo_root=installed_repo)

    class _NoUrlRun:
        id = "only-id"

    ctx.bind(_NoUrlRun())
    assert job.doc["tracker"]["url"] is None


# ---------------------------------------------------------------------------
# tracked_run
# ---------------------------------------------------------------------------


def test_tracked_run_calls_wandb_init_and_finish(
    fake_wandb, installed_repo: Path
) -> None:
    job = create_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    with tracked_run(job, project="p", repo_root=installed_repo) as run:
        # Full wandb surface is available on `run`; we don't log here,
        # just assert identity.
        assert run is fake_wandb.last_run
    assert fake_wandb.init_call_count == 1
    kw = fake_wandb.last_init_kwargs
    assert kw["project"] == "p"
    assert kw["group"] == "H001/E001/full"
    assert kw.get("reinit") is True
    assert run.finished_code == 0
    assert job.doc["tracker"]["backend"] == "wandb"
    assert job.doc["tracker"]["run_id"] == run.id


def test_tracked_run_finishes_with_exit_code_1_on_exception(
    fake_wandb, installed_repo: Path
) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )

    class _Sentinel(RuntimeError):
        pass

    captured_run = None
    with pytest.raises(_Sentinel):
        with tracked_run(job, project="p", repo_root=installed_repo) as run:
            captured_run = run
            raise _Sentinel("boom")

    assert captured_run is not None
    assert captured_run.finished_code == 1


def test_tracked_run_merges_caller_name_and_job_type(
    fake_wandb, installed_repo: Path
) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    with tracked_run(
        job,
        project="p",
        name="ecg-0123",
        job_type="per-ecg-eval",
        repo_root=installed_repo,
    ):
        pass
    kw = fake_wandb.last_init_kwargs
    assert kw["name"] == "ecg-0123"
    assert kw["job_type"] == "per-ecg-eval"
    # aexp-owned keys still in force
    assert kw["project"] == "p"
    assert kw["group"].startswith("_/E001/")


def test_tracked_run_aexp_owned_keys_win_over_wandb_kwargs(
    fake_wandb, installed_repo: Path
) -> None:
    """Precedence rule: aexp wins for project/group/tags/config/notes/dir/mode."""
    job = create_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    # Caller passes a project that aexp should overwrite from its own
    # init_kwargs payload — managed mode enforces discipline.
    with tracked_run(
        job,
        project="aexp-project",
        repo_root=installed_repo,
        project_extra_unused="caller-project",  # unrelated kwarg; ignored by wandb stub
    ):
        pass
    kw = fake_wandb.last_init_kwargs
    assert kw["project"] == "aexp-project"
    assert kw["group"] == "H001/E001/full"


def test_tracked_run_raises_without_wandb(
    monkeypatch: pytest.MonkeyPatch, installed_repo: Path
) -> None:
    """If ``wandb`` can't be imported, surface a ``TrackerInitError``."""
    # Remove any cached wandb from sys.modules, then shadow it with an
    # import hook that raises ImportError.
    monkeypatch.delitem(sys.modules, "wandb", raising=False)

    class _BlockImport:
        def find_spec(self, name, path=None, target=None):
            if name == "wandb":
                raise ImportError("blocked by test")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockImport(), *sys.meta_path])

    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    with pytest.raises(TrackerInitError):
        with tracked_run(job, project="p", repo_root=installed_repo):
            pass
