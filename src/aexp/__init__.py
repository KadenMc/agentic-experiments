"""agentic-experiments: Limina-fork + signac + W&B fusion layer.

Top-level public API. Import from here; sub-modules may be reorganized.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Single source of truth: read the version from installed package metadata so
# we never drift from ``pyproject.toml`` again. A hard-coded ``__version__``
# in this file bit us once — the module said "0.1.0" after we bumped
# ``pyproject.toml`` to "0.1.1" for the --dev-flag release.
try:
    __version__ = _pkg_version("agentic-experiments")
except PackageNotFoundError:  # pragma: no cover - only in uninstalled-source edge cases
    __version__ = "0.0.0+unknown"

# Install / bootstrap -------------------------------------------------------
# Artifact creation (H / E / F / T) ----------------------------------------
from aexp.artifacts import (
    ArtifactCreateError,
    ArtifactCreateResult,
    ThreadStatusUpdate,
    close_thread,
    new_experiment,
    new_finding,
    new_hypothesis,
    new_thread,
)
from aexp.backlinks import add_backlink
from aexp.install import (
    InstallAction,
    compute_vendor_sha,
    install_limina,
    is_limina_installed,
)

# Limina readers ------------------------------------------------------------
from aexp.limina_io import (
    ArtifactNotFoundError,
    ArtifactReadError,
    list_kb_artifacts,
    load_artifact,
    load_experiment,
    load_finding,
    load_hypothesis,
    load_thread,
)

# Linking + batch queries ---------------------------------------------------
from aexp.linking import (
    link_to_experiment,
    list_batches,
    runs_for_experiment,
    show_batch,
    summarize_run,
)

# Queue / materialization / sp-resolution ----------------------------------
from aexp.queue import (
    RunnerCommandMissing,
    SubprocessFailed,
    SweepParseError,
    add_many_to_queue,
    add_to_queue,
    clear_queue,
    list_queue,
    materialize_queue,
    parse_sweep,
    remove_from_queue,
    render_runner_command,
    resolve_sp,
    run_queue,
    run_queued,
)

# signac-backed run store ---------------------------------------------------
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

# Schema / types ------------------------------------------------------------
from aexp.schema import (
    BatchSelector,
    BatchSummary,
    Issue,
    LiminaArtifactRef,
    MaterializeResult,
    QueueEntry,
    RunLink,
    RunStatus,
    RunSummary,
    SupportingJobRun,
    SupportingRun,
    TrackerBinding,
    batch_slug,
)

# Trackers ------------------------------------------------------------------
from aexp.trackers import (
    NoopAdapter,
    RunHandle,
    RunRecord,
    TrackerAdapter,
    TrackerContext,
    TrackerInitError,
    bind_tracker,
    prepare_tracker,
    tracked_run,
)

# Validation ----------------------------------------------------------------
from aexp.validate import ValidateResult, validate_repo


def _lazy_wandb_attr(name: str):
    """Lazy accessor for wandb-dependent exports (``WandbAdapter``, sync helpers)."""
    import importlib
    module = importlib.import_module("aexp.trackers.wandb_adapter")
    return getattr(module, name)


def __getattr__(name: str):
    if name in ("WandbAdapter", "OfflineSyncResult", "find_offline_runs", "sync_offline_runs"):
        return _lazy_wandb_attr(name)
    raise AttributeError(f"module 'aexp' has no attribute {name!r}")


__all__ = [
    "__version__",
    # install
    "InstallAction",
    "compute_vendor_sha",
    "install_limina",
    "is_limina_installed",
    # artifacts (H/E/F/T creation + backlink patching + thread lifecycle)
    "ArtifactCreateError",
    "ArtifactCreateResult",
    "ThreadStatusUpdate",
    "add_backlink",
    "close_thread",
    "new_experiment",
    "new_finding",
    "new_hypothesis",
    "new_thread",
    # runs
    "RunNotFound",
    "RunStoreNotInitialized",
    "create_run",
    "find_runs",
    "get_run_store",
    "init_run_store",
    "mark_status",
    "open_run",
    "run_lifecycle",
    # linking
    "link_to_experiment",
    "list_batches",
    "runs_for_experiment",
    "show_batch",
    "summarize_run",
    # queue / materialization / sp-resolution
    "RunnerCommandMissing",
    "SubprocessFailed",
    "SweepParseError",
    "add_many_to_queue",
    "add_to_queue",
    "clear_queue",
    "list_queue",
    "materialize_queue",
    "parse_sweep",
    "remove_from_queue",
    "render_runner_command",
    "resolve_sp",
    "run_queue",
    "run_queued",
    # limina_io
    "ArtifactNotFoundError",
    "ArtifactReadError",
    "list_kb_artifacts",
    "load_artifact",
    "load_experiment",
    "load_finding",
    "load_hypothesis",
    "load_thread",
    # schema
    "BatchSelector",
    "BatchSummary",
    "Issue",
    "LiminaArtifactRef",
    "MaterializeResult",
    "QueueEntry",
    "RunLink",
    "RunStatus",
    "RunSummary",
    "SupportingJobRun",
    "SupportingRun",
    "TrackerBinding",
    "batch_slug",
    # trackers — preferred wandb surface first
    "TrackerContext",
    "prepare_tracker",
    "tracked_run",
    # trackers — adapter path
    "NoopAdapter",
    "RunHandle",
    "RunRecord",
    "TrackerAdapter",
    "TrackerInitError",
    "bind_tracker",
    # validate
    "ValidateResult",
    "validate_repo",
]
