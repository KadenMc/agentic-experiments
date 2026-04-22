"""signac-backed run store + the Limina-aware ``create_run`` / ``find_runs`` API.

Plan §6: thin wrappers that expose signac's ``Project`` / ``Job`` types
directly — we do *not* hide them — while owning two conventions:

1. State point auto-population: ``experiment_id`` is always injected into
   ``job.sp``; ``code_commit`` / ``code_dirty`` are injected by default
   (switchable via ``include_commit=False``).
2. Job-document layout: ``job.doc["limina"]`` carries the ``RunLink`` dict;
   ``job.doc["status"]`` tracks the lifecycle; ``job.doc["tracker"]`` is
   populated later by tracker adapters.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator

import signac

from aexp.schema import RunLink, RunStatus, iso_utc_now
from aexp.utils.git import get_git_provenance
from aexp.utils.paths import (
    find_repo_root,
    read_installed_marker,
    resolve_run_store_path,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunStoreNotInitialized(RuntimeError):
    """Raised when we cannot find an initialized signac project for this repo.

    Usually means ``install_limina`` hasn't been run yet.
    """


class RunNotFound(LookupError):
    """Raised when ``open_run`` can't resolve a job id to an existing job."""


# ---------------------------------------------------------------------------
# Run-store (signac project) lifecycle
# ---------------------------------------------------------------------------


def init_run_store(repo_root: str | Path, path: str = ".runs") -> signac.Project:
    """Initialize (or re-open) a signac project at ``<repo_root>/<path>``.

    Safe to call repeatedly: existing projects are returned as-is.
    """
    root = Path(repo_root)
    store = (root / path).resolve()
    store.mkdir(parents=True, exist_ok=True)
    try:
        return signac.get_project(path=str(store))
    except LookupError:
        return signac.init_project(path=str(store))


def get_run_store(repo_root: str | Path | None = None) -> signac.Project:
    """Return the signac project for a repo.

    If ``repo_root`` isn't supplied, walks up from ``cwd`` looking for a
    ``.git`` dir or an install marker. Reads the configured run-store path
    out of ``.aexp/installed.json`` if present.
    """
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    store = resolve_run_store_path(root)
    if not store.is_dir():
        raise RunStoreNotInitialized(
            f"no run store at {store}; did you run `aex install`?"
        )
    try:
        return signac.get_project(path=str(store))
    except LookupError as exc:
        raise RunStoreNotInitialized(
            f"directory {store} exists but is not a signac project; "
            "did you run `aex install`?"
        ) from exc


# ---------------------------------------------------------------------------
# Create / open / find
# ---------------------------------------------------------------------------


def _build_statepoint(
    *,
    experiment_id: str,
    hypothesis_id: str | None,
    sub_hypothesis_id: str | None,
    user_sp: dict[str, Any],
    include_commit: bool,
    repo_root: Path,
) -> dict[str, Any]:
    """Assemble the final state-point dict per plan §2.

    User-supplied keys win over defaults so callers can override (e.g. pin
    a specific commit for replay) — identity collisions are the user's
    problem, not ours.
    """
    sp: dict[str, Any] = {"experiment_id": experiment_id}
    if hypothesis_id is not None:
        sp["hypothesis_id"] = hypothesis_id
    if sub_hypothesis_id is not None:
        sp["sub_hypothesis_id"] = sub_hypothesis_id

    if include_commit:
        prov = get_git_provenance(repo_root)
        sp["code_commit"] = prov["commit"]
        sp["code_dirty"] = prov["dirty"]

    sp.update(user_sp)
    return sp


def create_run(
    *,
    experiment_id: str,
    statepoint: dict[str, Any] | None = None,
    hypothesis_id: str | None = None,
    sub_hypothesis_id: str | None = None,
    experiment_path: str | None = None,
    repo_root: str | Path | None = None,
    init_doc: dict[str, Any] | None = None,
    include_commit: bool = True,
) -> signac.job.Job:
    """Open (or create) a signac job linked to a Limina experiment.

    Parameters
    ----------
    experiment_id : str
        Limina ``E###`` id. Always mirrored into ``job.sp["experiment_id"]``
        and ``job.doc["limina"]["experiment_id"]``.
    statepoint : dict | None
        Caller-supplied identity params. Merged on top of the auto-populated
        defaults (``experiment_id``, optional ``code_commit`` / ``code_dirty``).
    hypothesis_id, sub_hypothesis_id : str | None
        If provided, mirrored into both ``sp`` and ``doc["limina"]``.
    experiment_path : str | None
        Repo-relative POSIX path of the experiment artifact. Stored on
        ``doc["limina"]`` for quick navigation; not required for function.
    repo_root : str | Path | None
        Consumer repo root. Defaults to ``find_repo_root()``.
    init_doc : dict | None
        Extra keys to merge into ``job.doc`` at creation time.
    include_commit : bool
        If ``True`` (default), add ``code_commit`` / ``code_dirty`` to
        ``sp`` so re-running at a new commit yields a new directory.

    Returns
    -------
    signac.job.Job
        The initialized job. Its workspace dir has been materialized and
        ``job.doc`` carries the Limina link + initial ``status='created'``.
    """
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    project = get_run_store(root)
    sp = _build_statepoint(
        experiment_id=experiment_id,
        hypothesis_id=hypothesis_id,
        sub_hypothesis_id=sub_hypothesis_id,
        user_sp=dict(statepoint or {}),
        include_commit=include_commit,
        repo_root=root,
    )

    job = project.open_job(sp)
    job.init()

    # Stamp Limina link + lifecycle metadata. Re-stamp is safe; values
    # are deterministic for a given job id.
    link = RunLink(
        experiment_id=experiment_id,
        experiment_path=experiment_path or _default_experiment_path(experiment_id),
        hypothesis_id=hypothesis_id,
        sub_hypothesis_id=sub_hypothesis_id,
    )
    job.doc["limina"] = link.model_dump()
    job.doc.setdefault("status", "created")
    job.doc.setdefault("created_at", iso_utc_now())

    if init_doc:
        for k, v in init_doc.items():
            job.doc[k] = v

    return job


def _default_experiment_path(experiment_id: str) -> str:
    """Best-effort default path string for a given E/F/etc. id.

    Used when callers don't pass ``experiment_path``. The on-disk file may
    not yet exist — validators catch that separately.
    """
    return f"kb/research/experiments/{experiment_id}-*.md"


def open_run(job_id: str, *, repo_root: str | Path | None = None) -> signac.job.Job:
    """Return an existing signac job by its id.

    Raises
    ------
    RunNotFound
        If no job with that id exists in the run store.
    """
    project = get_run_store(repo_root)
    try:
        return project.open_job(id=job_id)
    except (KeyError, LookupError) as exc:
        raise RunNotFound(f"no run with id {job_id!r} in run store") from exc


def find_runs(
    *,
    experiment_id: str | None = None,
    hypothesis_id: str | None = None,
    status: RunStatus | None = None,
    repo_root: str | Path | None = None,
    **sp_filters: Any,
) -> list[signac.job.Job]:
    """Return signac jobs filtered by Limina-link metadata and / or ``sp`` keys.

    ``experiment_id`` and ``hypothesis_id`` match either a top-level ``sp``
    key (the canonical path) or the ``doc["limina"]`` fields (fallback for
    runs created before the sp mirror convention). Status is matched against
    ``doc["status"]``.

    Extra keyword arguments are treated as exact-match ``sp`` filters.
    """
    project = get_run_store(repo_root)

    sp_query: dict[str, Any] = dict(sp_filters)
    if experiment_id is not None:
        sp_query["experiment_id"] = experiment_id
    if hypothesis_id is not None:
        sp_query["hypothesis_id"] = hypothesis_id

    # Start with the sp-filtered set when we have any sp constraints;
    # otherwise iterate all jobs.
    if sp_query:
        candidates: Iterator[signac.job.Job] = iter(project.find_jobs(filter=sp_query))
    else:
        candidates = iter(project)  # type: ignore[assignment]

    results: list[signac.job.Job] = []
    for job in candidates:
        # Apply status filter via the job document.
        if status is not None and job.doc.get("status") != status:
            continue
        # Back-compat fallback when sp doesn't carry the link but doc does.
        if experiment_id is not None and job.sp.get("experiment_id") != experiment_id:
            limina = job.doc.get("limina", {})
            if limina.get("experiment_id") != experiment_id:
                continue
        if hypothesis_id is not None and job.sp.get("hypothesis_id") != hypothesis_id:
            limina = job.doc.get("limina", {})
            if limina.get("hypothesis_id") != hypothesis_id:
                continue
        results.append(job)
    return results


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@contextmanager
def run_lifecycle(
    job: signac.job.Job,
    *,
    mark_started: bool = True,
) -> Iterator[signac.job.Job]:
    """Context manager that tracks a run's status transitions.

    On enter: writes ``doc["status"]='running'`` and ``doc["started_at']``.
    On clean exit: writes ``status='complete'``, ``ended_at``, ``wallclock_s``.
    On exception: writes ``status='failed'``, ``ended_at``, ``wallclock_s``,
    and re-raises.

    Parameters
    ----------
    job : signac.job.Job
    mark_started : bool
        If ``False``, leave status untouched on enter. Useful when the
        caller wants to control the "created -> running" transition itself.
    """
    if mark_started:
        job.doc["status"] = "running"
        job.doc.setdefault("started_at", iso_utc_now())

    t0 = perf_counter()
    try:
        yield job
    except Exception:
        job.doc["status"] = "failed"
        job.doc["ended_at"] = iso_utc_now()
        job.doc["wallclock_s"] = round(perf_counter() - t0, 3)
        raise
    else:
        job.doc["status"] = "complete"
        job.doc["ended_at"] = iso_utc_now()
        job.doc["wallclock_s"] = round(perf_counter() - t0, 3)


def mark_status(job: signac.job.Job, status: RunStatus) -> None:
    """Set ``job.doc['status']`` and update ``ended_at`` / ``wallclock_s`` sensibly.

    Useful for out-of-band transitions like ``abandoned``.
    """
    job.doc["status"] = status
    if status in ("complete", "failed", "abandoned"):
        job.doc.setdefault("ended_at", iso_utc_now())


__all__ = [
    "RunNotFound",
    "RunStoreNotInitialized",
    "create_run",
    "find_runs",
    "get_run_store",
    "init_run_store",
    "mark_status",
    "open_run",
    "run_lifecycle",
]
