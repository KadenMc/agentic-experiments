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

from aexp import __version__
from aexp.linking import (
    link_to_experiment as _link_to_experiment,
    list_batches as _list_batches,
    show_batch as _show_batch,
    summarize_run as _summarize_run,
)
from aexp.runs import (
    create_run as _create_run,
    find_runs as _find_runs,
    open_run as _open_run,
)
from aexp.trackers import NoopAdapter, TrackerInitError, bind_tracker as _bind_tracker
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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio (the Claude Code transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
