"""MCP server exposing the ``aexp`` Python API as typed tool calls.

Run as::

    python -m aexp.mcp_server

or through Claude Code's ``mcpServers`` block, which ``aexp install`` wires
into ``.claude/settings.json`` automatically.

Tools mirror the CLI surface but return structured dicts rather than
rich-formatted tables — the point of MCP is that Claude gets typed JSON
it can branch on instead of parsing `rich.Table` output.

Requires the ``[mcp]`` extra: ``pip install agentic-experiments[mcp]``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "aexp.mcp_server requires the 'mcp' package. "
        "Install with: pip install agentic-experiments[mcp]"
    ) from exc

from aexp.artifacts import (
    ArtifactCreateError,
)
from aexp.artifacts import (
    close_thread as _close_thread,
)
from aexp.artifacts import (
    new_experiment as _new_experiment,
)
from aexp.artifacts import (
    new_finding as _new_finding,
)
from aexp.artifacts import (
    new_hypothesis as _new_hypothesis,
)
from aexp.artifacts import (
    new_thread as _new_thread,
)
from aexp.kb_io import (
    ArtifactNotFoundError as _ArtifactNotFoundError,
)
from aexp.kb_io import (
    list_kb_artifacts as _list_kb_artifacts,
)
from aexp.kb_io import (
    load_thread as _load_thread,
)
from aexp.linking import (
    link_to_experiment as _link_to_experiment,
)
from aexp.linking import (
    list_batches as _list_batches,
)
from aexp.linking import (
    show_batch as _show_batch,
)
from aexp.linking import (
    summarize_run as _summarize_run,
)
from aexp.queue import (
    StopJobError as _StopJobError,
)
from aexp.queue import (
    SweepParseError,
)
from aexp.queue import (
    add_many_to_queue as _add_many_to_queue,
)
from aexp.queue import (
    add_to_queue as _add_to_queue,
)
from aexp.queue import (
    clear_queue as _clear_queue,
)
from aexp.queue import (
    list_queue as _list_queue,
)
from aexp.queue import (
    materialize_queue as _materialize_queue,
)
from aexp.queue import (
    parse_sweep as _parse_sweep,
)
from aexp.queue import (
    remove_from_queue as _remove_from_queue,
)
from aexp.queue import (
    stop_queued as _stop_queued,
)
from aexp.runs import (
    create_run as _create_run,
)
from aexp.runs import (
    find_runs as _find_runs,
)
from aexp.runs import (
    open_run as _open_run,
)
from aexp.trackers import NoopAdapter, TrackerInitError
from aexp.trackers import bind_tracker as _bind_tracker
from aexp.validate import validate_repo as _validate_repo

mcp = FastMCP("aexp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonable_job(job: Any) -> dict[str, Any]:
    """Collapse a signac job into a plain dict the MCP layer can serialize."""
    s = _summarize_run(job)
    return {
        "job_id": s.job_id,
        "short_id": s.job_id[:8],
        "experiment_id": s.experiment_id,
        "hypothesis_id": s.hypothesis_id,
        "status": s.status,
        "batch_slug": s.batch_slug,
        "tracker_url": s.tracker_url,
        "sp": dict(s.sp),
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "workspace": str(Path(job.path)),
    }


# ---------------------------------------------------------------------------
# Tool: new_hypothesis / new_experiment / new_finding
# ---------------------------------------------------------------------------


def _artifact_result_to_dict(result: Any, *, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "artifact_id": result.artifact_id,
        "path": result.path,
        "backlinks_patched": list(result.backlinks_patched),
        "backlinks_already_present": list(result.backlinks_already_present),
    }


@mcp.tool()
def new_hypothesis(
    title: str,
    artifact_id: str | None = None,
    extra_links: list[str] | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Create a new hypothesis (H###).

    Writes ``kb/research/hypotheses/H###-<slug>.md`` with a validator-clean
    skeleton (frontmatter, blockquote metadata, ``## Links`` pre-populated
    with ``ACTIVE`` and ``CHALLENGE``). Returns the new id + path.

    Args:
        title: Human-readable title; becomes the H1 heading + filename slug.
        artifact_id: Optional explicit H### id. Defaults to smallest unused.
        extra_links: Optional extra wikilink targets for ## Links (e.g. a
            prior hypothesis). Backlinks are NOT patched for extras.
        thread_id: Optional T### parent thread this hypothesis was promoted
            from. When set, the thread must exist on disk; its ## Links
            section is auto-patched to add ``- [[H###]]``.
    """
    try:
        result = _new_hypothesis(
            title=title,
            artifact_id=artifact_id,
            extra_links=extra_links,
            thread_id=thread_id,
        )
    except ArtifactCreateError as exc:
        return {"error": str(exc), "code": "artifact_create_error"}
    return _artifact_result_to_dict(result, kind="H")


@mcp.tool()
def new_experiment(
    title: str,
    hypothesis_id: str,
    artifact_id: str | None = None,
    extra_links: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new experiment (E###) under an existing hypothesis.

    Writes the experiment skeleton and patches the parent H###'s ``## Links``
    section with ``- [[E###]]`` so ``kb_validate`` passes.

    Args:
        title: Human-readable title.
        hypothesis_id: Parent H### id (must exist on disk).
        artifact_id: Optional explicit E### id.
        extra_links: Optional extra wikilink targets.
    """
    try:
        result = _new_experiment(
            title=title,
            hypothesis_id=hypothesis_id,
            artifact_id=artifact_id,
            extra_links=extra_links,
        )
    except ArtifactCreateError as exc:
        return {"error": str(exc), "code": "artifact_create_error"}
    return _artifact_result_to_dict(result, kind="E")


@mcp.tool()
def new_finding(
    title: str,
    hypothesis_id: str,
    experiment_id: str,
    impact: str = "MEDIUM",
    artifact_id: str | None = None,
    extra_links: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new finding (F###) citing a hypothesis + experiment.

    Writes the finding skeleton and patches both parent files' ``## Links``
    sections. The ``supporting_runs:`` frontmatter list stays empty — add it
    via the close-run / close-batch slash commands once a job id is known.

    Args:
        title: Human-readable title.
        hypothesis_id: Parent H### id.
        experiment_id: Parent E### id.
        impact: CRITICAL | HIGH | MEDIUM | LOW (default MEDIUM).
        artifact_id: Optional explicit F### id.
        extra_links: Optional extra wikilink targets.
    """
    try:
        result = _new_finding(
            title=title,
            hypothesis_id=hypothesis_id,
            experiment_id=experiment_id,
            impact=impact,
            artifact_id=artifact_id,
            extra_links=extra_links,
        )
    except ArtifactCreateError as exc:
        return {"error": str(exc), "code": "artifact_create_error"}
    return _artifact_result_to_dict(result, kind="F")


# ---------------------------------------------------------------------------
# Tool: new_thread / list_threads / show_thread / close_thread
# ---------------------------------------------------------------------------


@mcp.tool()
def new_thread(
    title: str,
    artifact_id: str | None = None,
    extra_links: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new thread (T###).

    A thread is a forward-looking research concern broader than a single
    hypothesis — it captures exploration that may spawn 2–5 hypotheses
    over its lifetime. Threads are NOT in the H→E→F enforcement chain;
    they're parent context. See ``docs/threads.md`` for the model.

    Args:
        title: Human-readable title; becomes the H1 heading + filename slug.
        artifact_id: Optional explicit T### id. Defaults to smallest unused.
        extra_links: Optional extra wikilink targets for ## Links.
    """
    try:
        result = _new_thread(
            title=title,
            artifact_id=artifact_id,
            extra_links=extra_links,
        )
    except ArtifactCreateError as exc:
        return {"error": str(exc), "code": "artifact_create_error"}
    return _artifact_result_to_dict(result, kind="T")


@mcp.tool()
def list_threads(
    status: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """List threads, optionally filtered by status or tag.

    Args:
        status: ``PROPOSED`` | ``EXPLORING`` | ``PROMOTED`` | ``CLOSED``.
        tag: Match against the ``tags`` frontmatter list.
    """
    from aexp.utils.paths import find_repo_root

    kb = find_repo_root() / "kb"
    out: list[dict[str, Any]] = []
    for t in _list_kb_artifacts(kb, kind="T"):
        t_status = str(t.metadata.get("Status", "") or "").strip()
        t_tags = t.metadata.get("tags") or []
        if status is not None and t_status != status:
            continue
        if tag is not None:
            tag_list = (
                t_tags if isinstance(t_tags, list) else [str(t_tags)]
            )
            if tag not in tag_list:
                continue
        out.append(
            {
                "thread_id": t.id,
                "title": t.title,
                "path": t.path,
                "status": t_status,
                "created": t.metadata.get("Created", ""),
                "last_updated": t.metadata.get("Last updated", ""),
                "tags": list(t_tags) if isinstance(t_tags, list) else [],
            }
        )
    return out


@mcp.tool()
def show_thread(thread_id: str) -> dict[str, Any]:
    """Return one thread's frontmatter + body."""
    from aexp.utils.paths import find_repo_root

    kb = find_repo_root() / "kb"
    try:
        t = _load_thread(thread_id, kb_root=kb)
    except _ArtifactNotFoundError as exc:
        return {"error": str(exc), "code": "artifact_not_found"}
    return {
        "thread_id": t.id,
        "title": t.title,
        "path": t.path,
        "status": t.metadata.get("Status", ""),
        "created": t.metadata.get("Created", ""),
        "last_updated": t.metadata.get("Last updated", ""),
        "tags": list(t.metadata.get("tags") or []),
        "body": t.body,
    }


@mcp.tool()
def close_thread(
    thread_id: str,
    conclusion: str | None = None,
    promoted: bool = False,
) -> dict[str, Any]:
    """Transition a thread to ``CLOSED`` (default) or ``PROMOTED``.

    Args:
        thread_id: T### id of the thread to close.
        conclusion: Markdown body for the thread's ``## Conclusion``
            section. If ``None``, existing body is preserved.
        promoted: If True, set status to ``PROMOTED`` (one or more
            hypotheses spawned, thread persists as parent context).
            Otherwise, status becomes ``CLOSED`` (decided not to
            pursue / out of scope / superseded).
    """
    target_status = "PROMOTED" if promoted else "CLOSED"
    try:
        result = _close_thread(
            thread_id, conclusion=conclusion, new_status=target_status
        )
    except ArtifactCreateError as exc:
        return {"error": str(exc), "code": "artifact_create_error"}
    return {
        "thread_id": result.thread_id,
        "path": result.path,
        "new_status": result.new_status,
        "conclusion_written": result.conclusion_written,
    }


# ---------------------------------------------------------------------------
# Tool: new_run
# ---------------------------------------------------------------------------


@mcp.tool()
def new_run(
    experiment_id: str,
    statepoint: dict[str, Any] | None = None,
    hypothesis_id: str | None = None,
    sub_hypothesis_id: str | None = None,
    experiment_path: str | None = None,
    include_commit: bool = True,
) -> dict[str, Any]:
    """Create (or reopen) a signac job linked to an experiment.

    Args:
        experiment_id: The E### id this run is testing.
        statepoint: Identity-defining params (model, condition, seed, ...).
        hypothesis_id: Optional H### the run tests; defaults to the experiment's primary.
        sub_hypothesis_id: Optional narrower hypothesis within the experiment.
        experiment_path: Optional repo-relative path of the experiment artifact.
        include_commit: If True (default), add code_commit + code_dirty to sp.

    Returns the job's full state point, doc, and workspace path.
    """
    job = _create_run(
        experiment_id=experiment_id,
        statepoint=statepoint or {},
        hypothesis_id=hypothesis_id,
        sub_hypothesis_id=sub_hypothesis_id,
        experiment_path=experiment_path,
        include_commit=include_commit,
    )
    return _jsonable_job(job)


# ---------------------------------------------------------------------------
# Tool: list_runs
# ---------------------------------------------------------------------------


@mcp.tool()
def list_runs(
    experiment_id: str | None = None,
    hypothesis_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List signac jobs filtered by experiment / hypothesis / status.

    Returns one dict per matching run with id, experiment, hypothesis,
    status, batch_slug, tracker_url, state point, and timestamps.
    """
    jobs = _find_runs(
        experiment_id=experiment_id,
        hypothesis_id=hypothesis_id,
        status=status,  # type: ignore[arg-type]
    )
    return [_jsonable_job(j) for j in jobs]


# ---------------------------------------------------------------------------
# Tool: list_batches
# ---------------------------------------------------------------------------


@mcp.tool()
def list_batches(experiment_id: str | None = None) -> list[dict[str, Any]]:
    """Group runs into batches by (experiment_id, condition) and return summaries.

    Returns one dict per distinct batch slice with count, status counts,
    selector, and the W&B group string (if any tracker was bound).
    """
    summaries = _list_batches(experiment_id=experiment_id)
    return [
        {
            "experiment_id": b.experiment_id,
            "batch_slug": b.batch_slug,
            "selector": dict(b.selector),
            "count": b.count,
            "status_counts": dict(b.status_counts),
            "tracker_group": b.tracker_group,
        }
        for b in summaries
    ]


# ---------------------------------------------------------------------------
# Tool: show_run
# ---------------------------------------------------------------------------


@mcp.tool()
def show_run(job_id: str) -> dict[str, Any]:
    """Return the full state point, doc, and workspace path for one run."""
    job = _open_run(job_id)
    return _jsonable_job(job)


# ---------------------------------------------------------------------------
# Tool: show_batch
# ---------------------------------------------------------------------------


@mcp.tool()
def show_batch(
    experiment_id: str,
    condition: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Return every run matching a batch selector as summary dicts."""
    selector: dict[str, Any] = {}
    if condition is not None:
        selector["condition"] = condition
    if model is not None:
        selector["model"] = model
    rows = _show_batch(experiment_id=experiment_id, selector=selector)
    return [
        {
            "job_id": r.job_id,
            "short_id": r.job_id[:8],
            "experiment_id": r.experiment_id,
            "hypothesis_id": r.hypothesis_id,
            "status": r.status,
            "batch_slug": r.batch_slug,
            "tracker_url": r.tracker_url,
            "sp": dict(r.sp),
            "started_at": r.started_at,
            "ended_at": r.ended_at,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Tool: link_run
# ---------------------------------------------------------------------------


@mcp.tool()
def link_run(
    job_id: str,
    experiment_id: str,
    hypothesis_id: str | None = None,
    sub_hypothesis_id: str | None = None,
    experiment_path: str | None = None,
) -> dict[str, Any]:
    """Retroactively stamp ``doc['aexp']`` onto an existing signac job."""
    job = _link_to_experiment(
        job_id,
        experiment_id=experiment_id,
        hypothesis_id=hypothesis_id,
        sub_hypothesis_id=sub_hypothesis_id,
        experiment_path=experiment_path,
    )
    return _jsonable_job(job)


# ---------------------------------------------------------------------------
# Tool: bind_tracker
# ---------------------------------------------------------------------------


@mcp.tool()
def bind_tracker(
    job_id: str,
    backend: str = "noop",
    project: str | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Attach a tracker run (noop or W&B) to an existing signac job.

    Args:
        job_id: 32-hex signac job id (or short prefix).
        backend: "noop" (default; local JSONL) or "wandb".
        project: Required for wandb backend.
        offline: For wandb on HPC nodes without internet.

    Returns the tracker binding dict that was written to job.doc["tracker"].
    """
    if backend == "noop":
        adapter: Any = NoopAdapter()
    elif backend == "wandb":
        from aexp.trackers import WandbAdapter

        if not project:
            return {
                "error": "project is required for --backend wandb",
                "code": "missing_project",
            }
        try:
            adapter = WandbAdapter()
        except TrackerInitError as exc:
            return {"error": str(exc), "code": "tracker_init_error"}
    else:
        return {"error": f"unknown backend {backend!r}", "code": "unknown_backend"}

    job = _open_run(job_id)
    handle = _bind_tracker(
        job,
        adapter,
        project=project or "aexp-default",
        offline=offline,
    )
    return {
        "backend": handle.backend,
        "run_id": handle.id,
        "url": handle.url,
        "project": handle.project,
        "group": handle.group,
        "job_id": job.id,
    }


# ---------------------------------------------------------------------------
# Tool: validate
# ---------------------------------------------------------------------------


@mcp.tool()
def validate(mode: str = "full") -> dict[str, Any]:
    """Validate the KB + signac run-link integrity.

    Args:
        mode: "full" (default), "kb-only", or "runs-only".

    Returns a dict with ``ok`` (bool), ``errors`` (list), ``warnings`` (list).
    """
    result = _validate_repo(mode=mode)  # type: ignore[arg-type]
    return {
        "ok": result.ok,
        "errors": [
            {
                "code": i.code,
                "message": i.message,
                "path": i.path,
                "detail": i.detail,
            }
            for i in result.errors
        ],
        "warnings": [
            {
                "code": i.code,
                "message": i.message,
                "path": i.path,
                "detail": i.detail,
            }
            for i in result.warnings
        ],
    }


# ---------------------------------------------------------------------------
# Tool: sync_offline (W&B)
# ---------------------------------------------------------------------------


@mcp.tool()
def sync_offline(dry_run: bool = False) -> dict[str, Any]:
    """Walk the run store and sync every W&B offline run.

    Requires the [wandb] extra. dry_run=True lists without syncing.
    """
    from aexp.trackers import sync_offline_runs
    from aexp.utils.paths import find_repo_root, resolve_run_store_path

    repo_root = find_repo_root()
    run_store = resolve_run_store_path(repo_root)
    results = sync_offline_runs(run_store, dry_run=dry_run)
    return {
        "run_store": str(run_store),
        "dry_run": dry_run,
        "results": [
            {
                "path": r.path,
                "ok": r.ok,
                "returncode": r.returncode,
                "stdout": r.stdout,
                "stderr": r.stderr,
            }
            for r in results
        ],
    }


# ---------------------------------------------------------------------------
# Tool: queue_add
# ---------------------------------------------------------------------------


def _queue_entry_to_dict(entry: Any) -> dict[str, Any]:
    return {
        "job_id": entry.job_id,
        "short_id": entry.job_id[:8],
        "experiment_id": entry.experiment_id,
        "hypothesis_id": entry.hypothesis_id,
        "status": entry.status,
        "tag": entry.tag,
        "queued_at": entry.queued_at,
        "sp": dict(entry.sp),
        "last_error": entry.last_error,
    }


def _job_to_queue_dict(job: Any) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "short_id": job.id[:8],
        "sp": dict(job.sp),
        "status": job.doc.get("status"),
        "tag": (job.doc.get("queue") or {}).get("tag"),
        "workspace": str(Path(job.path)),
    }


@mcp.tool()
def queue_add(
    experiment_id: str,
    statepoint: dict[str, Any] | None = None,
    sweep: str | None = None,
    hypothesis_id: str | None = None,
    tag: str | None = None,
    runner_hint: str | None = None,
    resolve_conditions: bool = True,
) -> dict[str, Any]:
    """Register one or more pending runs (``status="queued"``).

    Args:
        experiment_id: The E### the runs test.
        statepoint: Fixed sp values applied to every job.
        sweep: Optional Cartesian-sweep spec, e.g.
            ``"condition=full|classify_only, seed=0..3"``. Expanded and
            combined with ``statepoint``.
        hypothesis_id: Optional H### override.
        tag: Groups queued jobs for list/materialize filtering.
        runner_hint: Hint for default materialize runner.
        resolve_conditions: If True (default) and sp.condition names a
            block in the experiment's ``conditions:`` frontmatter, merge
            the block into sp before signac creates the job.

    Returns a dict with ``num_queued`` and ``jobs`` (list of
    short_id/sp/status/workspace per job).
    """
    base_sp = dict(statepoint or {})
    if sweep:
        try:
            sweep_dict = _parse_sweep(sweep)
        except SweepParseError as exc:
            return {"error": str(exc), "code": "invalid_sweep"}
        overlap = set(base_sp) & set(sweep_dict)
        if overlap:
            return {
                "error": (
                    f"sp and sweep share keys: {sorted(overlap)}; "
                    "put each key in exactly one"
                ),
                "code": "key_collision",
            }
        jobs = _add_many_to_queue(
            experiment_id=experiment_id,
            hypothesis_id=hypothesis_id,
            base_sp=base_sp,
            sweep=sweep_dict,
            tag=tag,
            runner_hint=runner_hint,
            resolve_conditions=resolve_conditions,
        )
    else:
        jobs = [
            _add_to_queue(
                experiment_id=experiment_id,
                hypothesis_id=hypothesis_id,
                statepoint=base_sp,
                tag=tag,
                runner_hint=runner_hint,
                resolve_conditions=resolve_conditions,
            )
        ]
    return {
        "num_queued": len(jobs),
        "tag": tag,
        "jobs": [_job_to_queue_dict(j) for j in jobs],
    }


# ---------------------------------------------------------------------------
# Tool: queue_list
# ---------------------------------------------------------------------------


@mcp.tool()
def queue_list(
    experiment_id: str | None = None,
    tag: str | None = None,
    include_terminal: bool = False,
) -> list[dict[str, Any]]:
    """List queue entries, optionally filtered by experiment or tag.

    Defaults to hiding jobs in a terminal status (``complete`` /
    ``failed`` / ``abandoned``). Pass ``include_terminal=True`` to see
    historical queue entries.
    """
    entries = _list_queue(
        experiment_id=experiment_id,
        tag=tag,
        include_terminal=include_terminal,
    )
    return [_queue_entry_to_dict(e) for e in entries]


# ---------------------------------------------------------------------------
# Tool: queue_remove
# ---------------------------------------------------------------------------


@mcp.tool()
def queue_remove(job_id: str) -> dict[str, Any]:
    """Mark one queued job ``abandoned`` without executing it."""
    job = _remove_from_queue(job_id)
    return {"job_id": job.id, "status": job.doc.get("status")}


# ---------------------------------------------------------------------------
# Tool: queue_stop
# ---------------------------------------------------------------------------


@mcp.tool()
def queue_stop(
    job_id: str,
    grace_s: float = 5.0,
    force: bool = False,
) -> dict[str, Any]:
    """Interrupt a running queued job (live ``aexp run-queued`` subprocess).

    Reads the live-process pointer that ``run_queued`` writes into the
    job doc, sends SIGTERM to the process group (default), polls during
    a configurable grace window, and escalates to SIGKILL if the runner
    ignores SIGTERM. Sets the job status to ``"stopped"`` (distinct
    from ``"failed"`` and ``"abandoned"``).

    Refuses if the recorded host is not the MCP host (signals don't
    cross hosts); ssh into the recording host and rerun there. Detects
    pid recycling on Linux via process-start-time fingerprinting.

    Args:
        job_id: Signac job id (full or short prefix).
        grace_s: Seconds to wait between SIGTERM and SIGKILL. Default 5.
            Set to 0 to skip the grace period.
        force: If True, skip SIGTERM and send SIGKILL straight away.
    """
    try:
        _stop_queued(job_id, grace_s=grace_s, force=force)
    except _StopJobError as exc:
        return {"error": str(exc), "code": "stop_failed"}
    return {"job_id": job_id, "status": "stopped"}


# ---------------------------------------------------------------------------
# Tool: queue_clear
# ---------------------------------------------------------------------------


@mcp.tool()
def queue_clear(
    experiment_id: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Bulk-abandon queued jobs matching the filter."""
    abandoned = _clear_queue(experiment_id=experiment_id, tag=tag)
    return {"num_abandoned": len(abandoned), "job_ids": abandoned}


# ---------------------------------------------------------------------------
# Tool: queue_materialize
# ---------------------------------------------------------------------------


@mcp.tool()
def queue_materialize(
    runner: str = "shell",
    output_path: str = "run_queue.sh",
    experiment_id: str | None = None,
    tag: str | None = None,
    slurm_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit a runner script covering every matching queue entry.

    ``runner`` is one of ``"shell"``, ``"slurm"``, ``"manual"``.
    ``slurm_kwargs`` maps flag keys to values (e.g.
    ``{"time": "04:00:00", "mem": "32G", "gpus": "1"}``) and becomes
    ``#SBATCH --<key>=<value>`` lines. Special key ``extra`` is appended
    verbatim.

    Returns a dict with the absolute output path, num_jobs, and the
    list of job ids baked into the script.

    Note: this does NOT execute anything. The user invokes the emitted
    script wherever the runtime env lives (often a different machine
    than the MCP host). See the ``aexp run-queued <job_id>`` CLI verb
    for the per-job execution primitive.
    """
    if runner not in ("shell", "slurm", "manual"):
        return {
            "error": f"unknown runner {runner!r}; expected shell|slurm|manual",
            "code": "unknown_runner",
        }
    try:
        result = _materialize_queue(
            runner=runner,  # type: ignore[arg-type]
            output_path=output_path,
            experiment_id=experiment_id,
            tag=tag,
            slurm_kwargs=slurm_kwargs,
        )
    except ValueError as exc:
        return {"error": str(exc), "code": "materialize_failed"}
    return {
        "output_path": result.output_path,
        "runner": result.runner,
        "num_jobs": result.num_jobs,
        "job_ids": list(result.job_ids),
    }


# ---------------------------------------------------------------------------
# Tools: jupyter_introspect_current / jupyter_parse_introspection
# ---------------------------------------------------------------------------
#
# The aexp MCP server runs on the laptop; the kernel whose identity we need
# is on the cluster, reachable only via the Jupyter MCP server (which has
# its own ``execute_code`` tool). Rather than cross-MCP-couple, this tool
# hands the agent a small Python snippet to dispatch via the live Jupyter
# MCP, then a companion tool to validate/parse the returned stdout.


_JUPYTER_INTROSPECT_RECIPE = (
    "from aexp.jupyter import init; import json; "
    "print(json.dumps(init().model_dump(), default=str))"
)


@mcp.tool()
def jupyter_introspect_current() -> dict[str, Any]:
    """Return the recipe for live-introspecting the currently connected Jupyter.

    The agent is expected to dispatch the ``recipe`` Python snippet via the
    Jupyter MCP's ``execute_code`` tool, then pass the resulting stdout to
    :func:`jupyter_parse_introspection`. The recipe runs entirely inside
    the connected kernel — it does NOT touch any cells or notebook state.

    Output dict:
        recipe: Python one-liner to dispatch.
        execute_with: name of the Jupyter MCP tool to use.
        then_call: name of the aexp MCP tool to parse the result.
        notes: short rationale for why this two-step dance exists.
    """
    return {
        "recipe": _JUPYTER_INTROSPECT_RECIPE,
        "execute_with": "mcp__jupyter*__execute_code",
        "then_call": "mcp__aexp__jupyter_parse_introspection",
        "notes": (
            "aexp.jupyter.init() introspects the kernel's own process: "
            "SLURM context (cgroup-derived), Jupyter URL/port/token "
            "(from JPY_PARENT_PID + list_running_servers), attached "
            "notebooks (/api/sessions), GPU residents (nvidia-smi), and "
            "sibling Jupyters. Side-effect free."
        ),
    }


@mcp.tool()
def jupyter_parse_introspection(raw_output: str) -> dict[str, Any]:
    """Parse the stdout of an ``aexp.jupyter.init()`` dispatch.

    Accepts the raw text from ``execute_code`` (which may include kernel
    banners or trailing whitespace), extracts the JSON payload, validates
    it against the :class:`SessionInfo` schema, and returns the structured
    dict on success.

    Output dict:
        ok: bool
        session: parsed SessionInfo dict (only when ok=True)
        error: human-readable failure reason (only when ok=False)
        raw_output: echoes the input for debugging (only when ok=False)
    """
    import json as _json

    text = (raw_output or "").strip()
    if not text:
        return {"ok": False, "error": "empty output", "raw_output": raw_output}
    # Find the first '{' / last '}' to tolerate kernel banners before/after.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {
            "ok": False,
            "error": "no JSON object found in output",
            "raw_output": raw_output,
        }
    try:
        payload = _json.loads(text[start : end + 1])
    except _json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"JSON decode failed: {exc}",
            "raw_output": raw_output,
        }

    try:
        from aexp.jupyter import SessionInfo
        info = SessionInfo.model_validate(payload)
    except ImportError as exc:
        return {"ok": False, "error": str(exc), "raw_output": raw_output}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"SessionInfo validation failed: {exc}",
            "raw_output": raw_output,
        }
    return {"ok": True, "session": info.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Tools: airgapped relay (airgapped_status / _pull / _push / ...)
# ---------------------------------------------------------------------------
#
# The aexp MCP server runs on the laptop -- which is exactly where the
# airgapped relay's SSH transport originates. These tools run whitelisted
# git/wandb commands on an internet-having HPC login node over SSH, on
# behalf of an agent whose compute node is network-isolated.
#
# ssh_host / remote_repo default to $AEXP_RELAY_SSH_HOST /
# $AEXP_RELAY_REMOTE_REPO (set them in the .mcp.json `env` block); the
# per-call params override the env for one call.


def _airgapped_call(
    op: str,
    *,
    args: list[str] | None = None,
    approve: bool = False,
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run one relay op; return a typed dict. Never raises.

    ``ok`` reports whether the SSH round-trip succeeded -- it is True even
    when ``returncode`` is non-zero (git ran and reported a result, e.g. a
    merge conflict). ``ok`` is False only for transport/validation/consent
    failures, with ``code`` naming the RelayError subclass.
    """
    from aexp.airgapped import RelayError, request

    try:
        result = request(
            op,
            args,
            ssh_host=ssh_host,
            remote_repo=remote_repo,
            approve=approve,
            timeout=timeout,
        )
    except RelayError as exc:
        return {
            "ok": False,
            "op": op,
            "error": str(exc),
            "code": type(exc).__name__,
        }
    return {
        "ok": True,
        "op": op,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "duration_s": result.duration_s,
        "request_id": result.request_id,
    }


@mcp.tool()
def airgapped_status(ssh_host: str | None = None) -> dict[str, Any]:
    """Check the airgapped relay's login node is reachable over SSH.

    Runs ``ssh <host> true``. Returns ``{ok, ssh_host}`` on success or
    ``{ok: False, error, code}`` if the login node is unreachable.
    """
    from aexp.airgapped import RelayError, check_connection

    try:
        host = check_connection(ssh_host=ssh_host)
    except RelayError as exc:
        return {"ok": False, "error": str(exc), "code": type(exc).__name__}
    return {"ok": True, "ssh_host": host}


@mcp.tool()
def airgapped_pull(
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run ``git pull --ff-only`` on the login node's repo over SSH."""
    return _airgapped_call(
        "git_pull", ssh_host=ssh_host, remote_repo=remote_repo, timeout=timeout
    )


@mcp.tool()
def airgapped_fetch(
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run ``git fetch --all --prune`` on the login node's repo over SSH."""
    return _airgapped_call(
        "git_fetch", ssh_host=ssh_host, remote_repo=remote_repo, timeout=timeout
    )


@mcp.tool()
def airgapped_repo_status(
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run ``git status --porcelain=v2`` on the login node's repo over SSH."""
    return _airgapped_call(
        "git_status", ssh_host=ssh_host, remote_repo=remote_repo, timeout=timeout
    )


@mcp.tool()
def airgapped_rebase(
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run ``git pull --rebase`` on the login node's repo over SSH.

    Recovers a no-conflict divergence; on a real conflict the rebase
    aborts and ``returncode`` is non-zero (inspect ``stdout``).
    """
    return _airgapped_call(
        "git_rebase", ssh_host=ssh_host, remote_repo=remote_repo, timeout=timeout
    )


@mcp.tool()
def airgapped_push(
    branch: str = "HEAD",
    remote: str = "origin",
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run ``git push <remote> <branch>`` on the login node over SSH.

    Defaults to ``git push origin HEAD``.
    """
    return _airgapped_call(
        "git_push",
        args=[remote, branch],
        ssh_host=ssh_host,
        remote_repo=remote_repo,
        timeout=timeout,
    )


@mcp.tool()
def airgapped_wandb_sync(
    approve: bool = False,
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run ``wandb sync --sync-all`` on the login node over SSH.

    Consent-required: this publishes run data to W&B. ``approve`` must be
    True, and you should confirm with the user before setting it. Called
    without ``approve=True`` it returns ``{ok: False, code:
    "RelayRejectedError"}`` and runs nothing.
    """
    return _airgapped_call(
        "wandb_sync",
        approve=approve,
        ssh_host=ssh_host,
        remote_repo=remote_repo,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio (the Claude Code transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
