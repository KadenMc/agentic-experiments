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
    new_experiment as _new_experiment,
    new_finding as _new_finding,
    new_hypothesis as _new_hypothesis,
)
from aexp.linking import (
    link_to_experiment as _link_to_experiment,
)
from aexp.queue import (
    SweepParseError,
    add_many_to_queue as _add_many_to_queue,
    add_to_queue as _add_to_queue,
    clear_queue as _clear_queue,
    list_queue as _list_queue,
    materialize_queue as _materialize_queue,
    parse_sweep as _parse_sweep,
    remove_from_queue as _remove_from_queue,
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
) -> dict[str, Any]:
    """Create a new Limina hypothesis (H###).

    Writes ``kb/research/hypotheses/H###-<slug>.md`` with a validator-clean
    skeleton (frontmatter, blockquote metadata, ``## Links`` pre-populated
    with ``ACTIVE`` and ``CHALLENGE``). Returns the new id + path.

    Args:
        title: Human-readable title; becomes the H1 heading + filename slug.
        artifact_id: Optional explicit H### id. Defaults to smallest unused.
        extra_links: Optional extra wikilink targets for ## Links (e.g. a
            prior hypothesis). Backlinks are NOT patched for extras.
    """
    try:
        result = _new_hypothesis(
            title=title,
            artifact_id=artifact_id,
            extra_links=extra_links,
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
    """Create a new Limina experiment (E###) under an existing hypothesis.

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
    """Create a new Limina finding (F###) citing a hypothesis + experiment.

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
    """Create (or reopen) a signac job linked to a Limina experiment.

    Args:
        experiment_id: Limina E### id this run is testing.
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
    """Retroactively stamp ``doc['limina']`` onto an existing signac job."""
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
    """Validate the Limina KB + signac run-link integrity.

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
        experiment_id: Limina E### the runs test.
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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio (the Claude Code transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
