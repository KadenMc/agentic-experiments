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

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Any

import signac

from aexp.schema import RunLink, RunStatus, iso_utc_now
from aexp.utils.git import get_git_provenance
from aexp.utils.paths import (
    find_repo_root,
    resolve_run_store_path,
)

# Default heartbeat interval (seconds). 30 is a good middle ground:
# - signac's atomic-write doc store handles 30s writes without contention.
# - liveness probes can detect a stalled run within a minute.
# - mid-job processes (real ML training) won't notice the I/O.
# Override per-run via ``run_lifecycle(..., heartbeat_s=)``; set to 0
# to disable. Override globally via ``AEXP_HEARTBEAT_S`` env var.
DEFAULT_HEARTBEAT_S: float = 30.0

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
    resolve_conditions: bool = True,
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
        If ``resolve_conditions`` and the statepoint contains a
        ``"condition"`` key matching a named block in the experiment's
        ``conditions:`` frontmatter, the block is merged into the sp
        before signac creates the job — so the resolved config is frozen
        to ``signac_statepoint.json``.
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
    resolve_conditions : bool
        If ``True`` (default) and ``statepoint["condition"]`` matches a
        named block in the experiment's ``conditions:`` frontmatter, merge
        the block into the sp *before* signac creates the job. Pass
        ``False`` for pure bare-label behavior (e.g. when queueing a job
        whose sp has already been resolved, or when the caller deliberately
        wants to freeze a label without its config). User-supplied sp
        values always win over condition defaults on collision.

    Returns
    -------
    signac.job.Job
        The initialized job. Its workspace dir has been materialized and
        ``job.doc`` carries the Limina link + initial ``status='created'``.
    """
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    project = get_run_store(root)

    user_sp = dict(statepoint or {})
    if resolve_conditions:
        # Lazy import to avoid a circular import — aexp.queue imports
        # create_run itself for queue registration.
        from aexp.queue import resolve_sp

        user_sp = resolve_sp(experiment_id, user_sp, kb_root=root / "kb")

    sp = _build_statepoint(
        experiment_id=experiment_id,
        hypothesis_id=hypothesis_id,
        sub_hypothesis_id=sub_hypothesis_id,
        user_sp=user_sp,
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


def _resolve_heartbeat_interval(explicit: float | None) -> float:
    """Pick the heartbeat interval: explicit > env var > module default.

    A value of ``0`` (or negative) disables the heartbeat. The env-var
    path lets cluster ops tune the cadence globally without touching
    consumer code (e.g. lower it on jobs whose runtime is bounded by
    a few minutes).
    """
    if explicit is not None:
        return max(0.0, float(explicit))
    raw = os.environ.get("AEXP_HEARTBEAT_S")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return DEFAULT_HEARTBEAT_S
    return DEFAULT_HEARTBEAT_S


@contextmanager
def run_lifecycle(
    job: signac.job.Job,
    *,
    mark_started: bool = True,
    heartbeat_s: float | None = None,
) -> Iterator[signac.job.Job]:
    """Context manager that tracks a run's status transitions.

    On enter: writes ``doc["status"]='running'`` and ``doc["started_at"]``,
    and starts a daemon heartbeat thread that touches
    ``doc["heartbeat_at"]`` (ISO-8601 UTC) every ``heartbeat_s`` seconds.
    On clean exit: writes ``status='complete'``, ``ended_at``,
    ``wallclock_s``, and stops the heartbeat thread.
    On exception: writes ``status='failed'``, ``ended_at``, ``wallclock_s``,
    stops the heartbeat thread, and re-raises.

    Heartbeat
    ---------
    The heartbeat is the answer to "how does a separate process tell
    whether this run is alive vs. wedged?" Without it, ``status='running'``
    is set once and never updated; consumers using the doc's mtime as a
    liveness signal get false-stale readings during jobs that are
    working hard (no doc writes during inference loops). The electricrag
    F.1 session lost real time to this misunderstanding.

    Parameters
    ----------
    job : signac.job.Job
    mark_started : bool
        If ``False``, leave status untouched on enter. Useful when the
        caller wants to control the "created -> running" transition
        itself.
    heartbeat_s : float | None
        Heartbeat interval in seconds. ``None`` (default) defers to
        ``AEXP_HEARTBEAT_S`` env var, then ``DEFAULT_HEARTBEAT_S`` (30 s).
        Set to ``0`` to disable.

    Notes
    -----
    The heartbeat thread is a daemon, so an unexpected interpreter exit
    (SIGKILL, ``os._exit``) won't leave it dangling. The thread also
    swallows write exceptions silently — if the signac doc-store lock
    contends or the workspace disappears, we'd rather fail noisily on
    the main path than mask it with a heartbeat-thread crash.
    """
    if mark_started:
        job.doc["status"] = "running"
        job.doc.setdefault("started_at", iso_utc_now())

    interval = _resolve_heartbeat_interval(heartbeat_s)
    stop_event = threading.Event() if interval > 0 else None
    hb_thread: threading.Thread | None = None
    hb_stopped = False

    def _heartbeat_loop() -> None:
        # Run a tight-but-bounded loop. We sleep on the stop_event so
        # exit happens within ~10ms of context-exit, not after a full
        # interval — important for tests and for fast-failing runs.
        while stop_event is not None and not stop_event.wait(interval):
            try:
                job.doc["heartbeat_at"] = iso_utc_now()
            except Exception:
                # Doc-store contention or workspace deletion — caller
                # will see the real failure on the main path. Don't
                # let a heartbeat-thread crash mask that.
                return

    def _stop_heartbeat() -> None:
        """Signal + join the heartbeat thread. Idempotent.

        MUST be called before the main thread writes terminal-status
        fields (``ended_at``, ``wallclock_s``, terminal ``status``).
        On Windows, the signac doc store uses atomic-write file rename;
        two threads writing simultaneously trip ``PermissionError`` on
        the JSON file. Stopping the heartbeat first eliminates the race.
        """
        nonlocal hb_stopped
        if hb_stopped:
            return
        hb_stopped = True
        if stop_event is not None:
            stop_event.set()
        if hb_thread is not None:
            hb_thread.join(timeout=1.0)

    if stop_event is not None:
        # Touch once on enter so consumers don't have to wait an interval
        # for the first liveness signal.
        try:
            job.doc["heartbeat_at"] = iso_utc_now()
        except Exception:
            pass
        hb_thread = threading.Thread(
            target=_heartbeat_loop, daemon=True, name=f"aexp-hb-{job.id[:8]}"
        )
        hb_thread.start()

    t0 = perf_counter()
    try:
        yield job
    except Exception:
        # Stop heartbeat BEFORE writing terminal status to avoid a
        # Windows doc-store file-lock race (Python 3.13 + signac's
        # atomic-write rename). Idempotent + safe under further
        # exceptions in the writes below.
        _stop_heartbeat()
        job.doc["status"] = "failed"
        job.doc["ended_at"] = iso_utc_now()
        job.doc["wallclock_s"] = round(perf_counter() - t0, 3)
        raise
    else:
        _stop_heartbeat()
        job.doc["status"] = "complete"
        job.doc["ended_at"] = iso_utc_now()
        job.doc["wallclock_s"] = round(perf_counter() - t0, 3)
    finally:
        # Belt-and-suspenders: if neither except nor else branch ran
        # (shouldn't happen with the contextmanager protocol, but
        # defensive), guarantee the heartbeat thread is dead before
        # we leave the context.
        _stop_heartbeat()


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
