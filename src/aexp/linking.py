"""Batch queries + retroactive run-to-experiment linking helpers.

A *batch* (plan §2) is not a persisted artifact; it is a query-level slice
over signac jobs defined by a shared ``(experiment_id, condition, ...)``
state-point signature, mapping 1:1 to a W&B group string.

This module exposes three capabilities:

1. :func:`runs_for_experiment` — convenience wrapper over ``find_runs``.
2. :func:`list_batches` / :func:`show_batch` — distinct ``(experiment,
   condition)`` slices rolled up to :class:`BatchSummary`.
3. :func:`link_to_experiment` — retroactively stamp ``job.doc["aexp"]``
   onto a job that was created without a link (or to repoint it).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import signac

from aexp.runs import find_runs, open_run
from aexp.schema import (
    BatchSummary,
    RunLink,
    RunStatus,
    RunSummary,
    batch_slug,
    read_run_link,
    write_run_link,
)

# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def runs_for_experiment(
    experiment_id: str,
    *,
    repo_root: str | Path | None = None,
) -> list[signac.job.Job]:
    """Return every job linked to a given ``E###``."""
    return find_runs(experiment_id=experiment_id, repo_root=repo_root)


def summarize_run(job: signac.job.Job) -> RunSummary:
    """Flatten a signac job into a :class:`RunSummary` row."""
    link = read_run_link(job.doc)
    tracker = dict(job.doc.get("tracker", {}))
    hyp = job.sp.get("hypothesis_id") or link.get("hypothesis_id")
    exp = job.sp.get("experiment_id") or link.get("experiment_id")
    condition = job.sp.get("condition")
    slug = batch_slug(
        hypothesis_id=hyp,
        experiment_id=exp,
        condition=condition,
        fallback=job.id[:8],
    ) if exp else None
    return RunSummary(
        job_id=job.id,
        experiment_id=exp,
        hypothesis_id=hyp,
        status=job.doc.get("status"),
        batch_slug=slug,
        tracker_url=tracker.get("url"),
        sp=dict(job.sp),
        started_at=job.doc.get("started_at"),
        ended_at=job.doc.get("ended_at"),
    )


# ---------------------------------------------------------------------------
# Batches (query-level, not persisted)
# ---------------------------------------------------------------------------


def _selector_key(job: signac.job.Job, selector_keys: tuple[str, ...]) -> tuple:
    """Deterministic batch key derived from selected ``sp`` values."""
    return tuple(job.sp.get(k) for k in selector_keys)


def list_batches(
    *,
    experiment_id: str | None = None,
    selector_keys: tuple[str, ...] = ("condition",),
    repo_root: str | Path | None = None,
) -> list[BatchSummary]:
    """Group runs into batches by their ``selector_keys`` slice.

    A batch is defined by a distinct tuple of values for the chosen state-point
    keys (plus ``experiment_id``). Default selector is ``("condition",)`` —
    the most common mapping to W&B groups.

    Parameters
    ----------
    experiment_id : str | None
        If given, restrict grouping to this experiment's runs.
    selector_keys : tuple[str, ...]
        State-point keys that define batch identity. Change to e.g.
        ``("condition", "model")`` for finer slices.
    repo_root : str | Path | None
        Consumer repo root.
    """
    jobs = find_runs(experiment_id=experiment_id, repo_root=repo_root)

    # Group by (experiment_id, *selector_values)
    groups: dict[tuple, list[signac.job.Job]] = defaultdict(list)
    for job in jobs:
        exp = job.sp.get("experiment_id") or read_run_link(job.doc).get("experiment_id")
        if exp is None:
            continue
        key = (exp,) + _selector_key(job, selector_keys)
        groups[key].append(job)

    summaries: list[BatchSummary] = []
    for key, batch_jobs in sorted(groups.items()):
        exp = key[0]
        sel = dict(zip(selector_keys, key[1:], strict=True))
        first = batch_jobs[0]
        hyp = first.sp.get("hypothesis_id") or read_run_link(first.doc).get("hypothesis_id")
        cond = sel.get("condition")
        slug = batch_slug(
            hypothesis_id=hyp,
            experiment_id=exp,
            condition=cond,
            fallback=first.id[:8],
        )
        status_counts: Counter[RunStatus] = Counter()
        for j in batch_jobs:
            status_counts[j.doc.get("status") or "created"] += 1
        tracker_group: str | None = None
        for j in batch_jobs:
            tgroup = j.doc.get("tracker", {}).get("group")
            if tgroup:
                tracker_group = tgroup
                break
        summaries.append(
            BatchSummary(
                experiment_id=exp,
                batch_slug=slug,
                selector=sel,
                count=len(batch_jobs),
                status_counts=dict(status_counts),
                tracker_group=tracker_group,
            )
        )
    return summaries


def show_batch(
    *,
    experiment_id: str,
    selector: dict[str, Any],
    repo_root: str | Path | None = None,
) -> list[RunSummary]:
    """Return :class:`RunSummary` rows for every run matching a selector.

    Exact-match sp filter. Typical call:
    ``show_batch(experiment_id="E018", selector={"condition": "full"})``.
    """
    jobs = find_runs(
        experiment_id=experiment_id,
        repo_root=repo_root,
        **selector,
    )
    return [summarize_run(j) for j in jobs]


# ---------------------------------------------------------------------------
# Retroactive linking
# ---------------------------------------------------------------------------


def link_to_experiment(
    job_id: str,
    *,
    experiment_id: str,
    hypothesis_id: str | None = None,
    sub_hypothesis_id: str | None = None,
    experiment_path: str | None = None,
    repo_root: str | Path | None = None,
) -> signac.job.Job:
    """Stamp (or overwrite) ``job.doc["aexp"]`` on an existing job.

    Used by the ``aex link`` command to retroactively link jobs that were
    created outside ``create_run`` (e.g. by direct signac calls from notebooks).
    """
    job = open_run(job_id, repo_root=repo_root)
    link = RunLink(
        experiment_id=experiment_id,
        experiment_path=experiment_path or f"kb/research/experiments/{experiment_id}-*.md",
        hypothesis_id=hypothesis_id,
        sub_hypothesis_id=sub_hypothesis_id,
    )
    write_run_link(job.doc, link.model_dump())
    return job


__all__ = [
    "link_to_experiment",
    "list_batches",
    "runs_for_experiment",
    "show_batch",
    "summarize_run",
]
