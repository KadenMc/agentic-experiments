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
from typing import TYPE_CHECKING, Any

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
from aexp.utils.atomic import doc_op_with_retry

if TYPE_CHECKING:
    import signac

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
    """Flatten a signac job into a :class:`RunSummary` row.

    Reads the job's doc once -- via ``job.doc()``, signac's own "give me
    an unsynced plain-dict copy" call -- instead of five separate
    ``job.doc.get(...)`` round trips. Each of those was its own
    open-read-parse of ``signac_job_document.json`` and its own chance to
    collide with a live heartbeat thread on Windows; one snapshot cuts
    both the I/O and the race surface, at the cost of the five fields
    now reflecting one coherent instant rather than five independent
    ones (an improvement for a projection like this, not a regression).
    """
    doc = doc_op_with_retry(lambda: job.doc())
    link = read_run_link(doc)
    tracker = dict(doc.get("tracker", {}))
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
        status=doc.get("status"),
        batch_slug=slug,
        tracker_url=tracker.get("url"),
        sp=dict(job.sp),
        started_at=doc.get("started_at"),
        ended_at=doc.get("ended_at"),
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

    # One doc snapshot per job, taken up front and reused for every read
    # below (grouping, status tally, tracker-group lookup) instead of
    # re-opening signac_job_document.json on each `.doc.get(...)` call.
    # Same trade as `summarize_run`: fewer disk round trips and less
    # Windows heartbeat-race surface, in exchange for each job's fields
    # reflecting one coherent instant instead of whichever instant each
    # separate read happened to land on.
    # doc_op_with_retry calls the closure immediately, before `job` is
    # rebound by the next comprehension step, so capturing the loop
    # variable is safe here.
    docs: dict[str, dict[str, Any]] = {
        job.id: doc_op_with_retry(lambda: job.doc())  # noqa: B023
        for job in jobs
    }

    # Group by (experiment_id, *selector_values)
    groups: dict[tuple, list[signac.job.Job]] = defaultdict(list)
    for job in jobs:
        exp = job.sp.get("experiment_id") or read_run_link(docs[job.id]).get(
            "experiment_id"
        )
        if exp is None:
            continue
        key = (exp,) + _selector_key(job, selector_keys)
        groups[key].append(job)

    summaries: list[BatchSummary] = []
    for key, batch_jobs in sorted(groups.items()):
        exp = key[0]
        sel = dict(zip(selector_keys, key[1:], strict=True))
        first = batch_jobs[0]
        hyp = first.sp.get("hypothesis_id") or read_run_link(docs[first.id]).get(
            "hypothesis_id"
        )
        cond = sel.get("condition")
        slug = batch_slug(
            hypothesis_id=hyp,
            experiment_id=exp,
            condition=cond,
            fallback=first.id[:8],
        )
        status_counts: Counter[RunStatus] = Counter()
        for j in batch_jobs:
            status_counts[docs[j.id].get("status") or "created"] += 1
        tracker_group: str | None = None
        for j in batch_jobs:
            tgroup = (docs[j.id].get("tracker") or {}).get("group")
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
    # `aex link` can be pointed at a job while it's still running (e.g.
    # fixing a mislinked run mid-flight), so both the write and the
    # status read below can race a live heartbeat thread on Windows.
    # write_run_link does a setitem + a pop; wrapping the whole call
    # is safe to retry as a unit since both of its steps are idempotent.
    doc_op_with_retry(lambda: write_run_link(job.doc, link.model_dump()))
    # If the job is already terminal, re-promote so the ledger entry's
    # run_link field reflects the new linkage. Without this, the ledger
    # projection would lag the on-disk job.doc until the next backfill.
    # Idempotent — re-promotion overwrites the entry.
    from aexp.runs import TERMINAL_STATUSES
    if doc_op_with_retry(lambda: job.doc.get("status")) in TERMINAL_STATUSES:
        try:
            from aexp.ledger import promote_to_ledger
            promote_to_ledger(job, repo_root=repo_root)
        except Exception as exc:  # pragma: no cover - defensive
            import sys as _sys
            print(
                f"[aexp ledger] re-promote after link failed for {job.id}: {exc}",
                file=_sys.stderr,
            )
    return job


__all__ = [
    "link_to_experiment",
    "list_batches",
    "runs_for_experiment",
    "show_batch",
    "summarize_run",
]
