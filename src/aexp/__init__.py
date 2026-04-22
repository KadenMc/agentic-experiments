"""agentic-experiments: Limina-fork + signac + W&B fusion layer.

Top-level public API. Import from here; sub-modules may be reorganized.
"""
from __future__ import annotations

__version__ = "0.1.0"

# Install / bootstrap -------------------------------------------------------
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
)

# Linking + batch queries ---------------------------------------------------
from aexp.linking import (
    link_to_experiment,
    list_batches,
    runs_for_experiment,
    show_batch,
    summarize_run,
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
    TrackerInitError,
    bind_tracker,
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
    # limina_io
    "ArtifactNotFoundError",
    "ArtifactReadError",
    "list_kb_artifacts",
    "load_artifact",
    "load_experiment",
    "load_finding",
    "load_hypothesis",
    # schema
    "BatchSelector",
    "BatchSummary",
    "Issue",
    "LiminaArtifactRef",
    "RunLink",
    "RunStatus",
    "RunSummary",
    "SupportingJobRun",
    "SupportingRun",
    "TrackerBinding",
    "batch_slug",
    # trackers
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
