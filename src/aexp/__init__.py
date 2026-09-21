"""agentic-experiments: research harness + signac + W&B fusion layer.

Top-level public API. Import from here; sub-modules may be reorganized.

Public names are resolved lazily (PEP 562) — see ``_LAZY_EXPORTS`` for why.
"""
from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any

# Single source of truth: read the version from installed package metadata so
# we never drift from ``pyproject.toml`` again. A hard-coded ``__version__``
# in this file bit us once — the module said "0.1.0" after we bumped
# ``pyproject.toml`` to "0.1.1" for the --dev-flag release.
try:
    __version__ = _pkg_version("agentic-experiments")
except PackageNotFoundError:  # pragma: no cover - only in uninstalled-source edge cases
    __version__ = "0.0.0+unknown"


# Public exports, grouped by the module that defines them, resolved on first
# attribute access rather than imported at module scope.
#
# Why lazy: importing *anything* from this package used to cost ~506 MB of
# private commit, because this module imported the run-store layer eagerly and
# ``aexp.runs`` -> ``signac`` -> ``synced_collections`` -> ``numpy``. Python
# initializes a parent package before any of its submodules, so that cost was
# charged even to ``from aexp.utils.atomic import atomic_write`` — a caller
# that wanted one small file-write helper and no numerical stack at all. Every
# submodule cost the same; there was no cheap corner of the package.
#
# This is a *dependency-graph* fix rather than a memory tuning knob. The
# separate BLAS thread cap on the MCP server launcher already reclaims the bulk
# of those megabytes, and with it applied this saves only ~15 MB more. What it
# buys is that ``aexp.utils.*`` and ``aexp.schema`` are importable standalone:
# a consumer of one helper no longer has to either pay for numpy or
# independently know to set an OpenBLAS environment variable.
#
# The public API is unchanged. ``from aexp import create_run``,
# ``import aexp; aexp.create_run``, ``aexp.runs`` as a submodule attribute, and
# ``from aexp.runs import create_run`` all behave exactly as they did.
_LAZY_EXPORTS: dict[str, tuple[str, ...]] = {
    # Artifact creation (H / E / F / T)
    "aexp.artifacts": (
        "ArtifactCreateError",
        "ArtifactCreateResult",
        "ThreadStatusUpdate",
        "close_thread",
        "new_experiment",
        "new_finding",
        "new_hypothesis",
        "new_thread",
    ),
    "aexp.backlinks": ("add_backlink",),
    # Install / bootstrap
    "aexp.install": (
        "InstallAction",
        "InstallRefused",
        "compute_scaffold_sha",
        "install_scaffold",
        "is_scaffold_installed",
    ),
    # kb/ artifact readers
    "aexp.kb_io": (
        "ArtifactNotFoundError",
        "ArtifactReadError",
        "list_kb_artifacts",
        "load_artifact",
        "load_experiment",
        "load_finding",
        "load_hypothesis",
        "load_thread",
    ),
    # Linking + batch queries
    "aexp.linking": (
        "link_to_experiment",
        "list_batches",
        "runs_for_experiment",
        "show_batch",
        "summarize_run",
    ),
    # Queue / materialization / sp-resolution
    "aexp.queue": (
        "DuplicatePendingJobWarning",
        "RunnerCommandMissing",
        "StopJobError",
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
        "stop_queued",
    ),
    # signac-backed run store
    "aexp.runs": (
        "RunNotFound",
        "RunStoreNotInitialized",
        "create_run",
        "find_runs",
        "get_run_store",
        "init_run_store",
        "mark_status",
        "open_run",
        "run_lifecycle",
    ),
    # Schema / types
    "aexp.schema": (
        "ArtifactRef",
        "BatchSelector",
        "BatchSummary",
        "Issue",
        "MaterializeResult",
        "QueueEntry",
        "RunLink",
        "RunStatus",
        "RunSummary",
        "SupportingJobRun",
        "SupportingRun",
        "TrackerBinding",
        "batch_slug",
    ),
    # Trackers
    "aexp.trackers": (
        "NoopAdapter",
        "RunHandle",
        "RunRecord",
        "TrackerAdapter",
        "TrackerContext",
        "TrackerInitError",
        "bind_tracker",
        "prepare_tracker",
        "tracked_run",
    ),
    # Trackers — wandb-dependent surface, behind the [wandb] extra. This part
    # was already lazy before the rest of the package joined it.
    "aexp.trackers.wandb_adapter": (
        "OfflineSyncResult",
        "WandbAdapter",
        "find_offline_runs",
        "sync_offline_runs",
    ),
    # Validation
    "aexp.validate": ("ValidateResult", "validate_repo"),
}

# Flattened {public name -> defining module}, derived from the grouping above
# so the two cannot drift. ``tests/test_lazy_init.py`` asserts this covers
# ``__all__`` exactly and that every name really resolves.
_LAZY: dict[str, str] = {
    name: module for module, names in _LAZY_EXPORTS.items() for name in names
}

if TYPE_CHECKING:
    # Type checkers and IDEs resolve the real names here; at runtime they are
    # supplied by ``__getattr__`` below. This block is what preserves strict
    # typing and autocomplete across the lazy boundary.
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
        InstallRefused,
        compute_scaffold_sha,
        install_scaffold,
        is_scaffold_installed,
    )
    from aexp.kb_io import (
        ArtifactNotFoundError,
        ArtifactReadError,
        list_kb_artifacts,
        load_artifact,
        load_experiment,
        load_finding,
        load_hypothesis,
        load_thread,
    )
    from aexp.linking import (
        link_to_experiment,
        list_batches,
        runs_for_experiment,
        show_batch,
        summarize_run,
    )
    from aexp.queue import (
        DuplicatePendingJobWarning,
        RunnerCommandMissing,
        StopJobError,
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
        stop_queued,
    )
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
    from aexp.schema import (
        ArtifactRef,
        BatchSelector,
        BatchSummary,
        Issue,
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

    # Deliberately absent from ``__all__`` -- they were reachable only through
    # ``__getattr__`` before this module went lazy, and widening the star-import
    # surface would be an API change. The noqa marks them as type-checking aids
    # rather than re-exports.
    from aexp.trackers.wandb_adapter import (
        OfflineSyncResult,  # noqa: F401
        WandbAdapter,  # noqa: F401
        find_offline_runs,  # noqa: F401
        sync_offline_runs,  # noqa: F401
    )
    from aexp.validate import ValidateResult, validate_repo


def __getattr__(name: str) -> Any:
    """Resolve a public export — or a submodule — on first access (PEP 562)."""
    module = _LAZY.get(name)
    if module is not None:
        value = getattr(importlib.import_module(module), name)
    else:
        # ``import aexp; aexp.runs`` worked back when this module imported its
        # submodules eagerly, because that bound them as attributes. Keep it
        # working now that it does not.
        try:
            value = importlib.import_module(f"aexp.{name}")
        except ModuleNotFoundError as exc:
            # Only *this* name being absent is an AttributeError. A missing
            # third-party dependency inside a real submodule (``wandb``, say)
            # must keep surfacing as the ImportError it is, rather than being
            # flattened into a misleading "no attribute" message.
            if exc.name != f"aexp.{name}":
                raise
            raise AttributeError(f"module 'aexp' has no attribute {name!r}") from None

    # Cache on the module so repeat access skips this function entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include the lazy names so ``dir(aexp)`` and tab-completion stay useful."""
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "__version__",
    # install
    "InstallAction",
    "InstallRefused",
    "compute_scaffold_sha",
    "install_scaffold",
    "is_scaffold_installed",
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
    "DuplicatePendingJobWarning",
    "RunnerCommandMissing",
    "StopJobError",
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
    "stop_queued",
    # kb_io
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
    "ArtifactRef",
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
