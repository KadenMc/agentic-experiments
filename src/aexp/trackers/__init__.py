"""Tracker adapters for agentic-experiments.

The :class:`TrackerAdapter` ABC is the public contract; concrete adapters
live alongside it. ``NoopAdapter`` is always available and writes a local
JSONL event log into ``job.workspace()/tracker_log/``. ``WandbAdapter`` is
optional — behind ``pip install agentic-experiments[wandb]`` — and is lazy
imported only when constructed.
"""
from __future__ import annotations

from aexp.trackers.base import (
    RunHandle,
    RunRecord,
    TrackerAdapter,
    TrackerInitError,
    bind_tracker,
)
from aexp.trackers.noop_adapter import NoopAdapter


def _wandb_adapter():
    """Lazy import to avoid a hard import of wandb."""
    from aexp.trackers.wandb_adapter import WandbAdapter

    return WandbAdapter


__all__ = [
    "NoopAdapter",
    "RunHandle",
    "RunRecord",
    "TrackerAdapter",
    "TrackerInitError",
    "bind_tracker",
    "_wandb_adapter",
]


_LAZY = {
    "WandbAdapter": "aexp.trackers.wandb_adapter:WandbAdapter",
    "OfflineSyncResult": "aexp.trackers.wandb_adapter:OfflineSyncResult",
    "find_offline_runs": "aexp.trackers.wandb_adapter:find_offline_runs",
    "sync_offline_runs": "aexp.trackers.wandb_adapter:sync_offline_runs",
}


def __getattr__(name: str):
    """Expose wandb-side helpers on-demand without importing wandb at module load."""
    if name in _LAZY:
        mod_name, attr = _LAZY[name].split(":", 1)
        import importlib

        return getattr(importlib.import_module(mod_name), attr)
    raise AttributeError(f"module 'aexp.trackers' has no attribute {name!r}")
