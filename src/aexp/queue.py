"""Organizational queue + runner-script materialization + sp resolution.

The queue lets an agent register N pending experiment runs on one machine
(e.g. a laptop via MCP) and materialize them as a runner script (shell /
slurm / manual) executed on another (e.g. an HPC cluster the agent can't
directly access). Status reconciliation happens through signac's on-disk
``job.doc`` — whatever machine executes a job writes its status; whatever
machine reads ``aexp queue list`` sees whatever's on its local filesystem.

Two orthogonal pieces live here:

1. **Queue CRUD + materialization** — :func:`add_to_queue`,
   :func:`add_many_to_queue`, :func:`list_queue`, :func:`remove_from_queue`,
   :func:`clear_queue`, :func:`materialize_queue`, :func:`run_queued`.

2. **State-point resolution** — :func:`resolve_sp` merges a named
   ``conditions.<name>`` block from the linked experiment's frontmatter
   into the caller's sp dict. Used by both :func:`add_to_queue` and
   (indirectly, via lazy import) by :func:`aexp.create_run`. The resolved
   sp is what signac freezes to ``signac_statepoint.json`` — a later edit
   to the experiment's ``conditions`` block cannot retroactively change
   what was captured at queue-time. That's the drift-proof provenance
   property that makes condition *labels* safe to use.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Literal

import signac

from aexp.limina_io import ArtifactNotFoundError, load_experiment
from aexp.runs import (
    RunNotFound,
    create_run,
    find_runs,
    mark_status,
    open_run,
    run_lifecycle,
)
from aexp.schema import MaterializeResult, QueueEntry, iso_utc_now
from aexp.utils.atomic import atomic_write
from aexp.utils.paths import find_repo_root


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunnerCommandMissing(RuntimeError):
    """Raised by :func:`run_queued` when the linked experiment declares no
    ``runner_command`` and no per-job override is present."""


class SubprocessFailed(RuntimeError):
    """Raised by :func:`run_queued` when the runner subprocess exits non-zero.
    ``run_lifecycle`` catches this and transitions the job to ``failed``;
    the stderr tail is captured into ``job.doc['queue']['last_error']`` first.
    """


class SweepParseError(ValueError):
    """Raised by the CLI when a ``--sweep`` string can't be parsed."""


# ---------------------------------------------------------------------------
# sp resolution
# ---------------------------------------------------------------------------


def resolve_sp(
    experiment_id: str,
    user_sp: dict[str, Any],
    *,
    kb_root: Path,
) -> dict[str, Any]:
    """Merge the named ``conditions.<name>`` block into ``user_sp``.

    If ``user_sp`` has a ``"condition"`` key AND the experiment's frontmatter
    declares a ``conditions:`` block AND that block contains a key matching
    ``user_sp["condition"]``, merge the block into ``user_sp`` — user keys
    win on collision, so ``--sp condition=full,max_turns=16`` overrides
    ``max_turns=12`` from the ``full`` block.

    Passes through unchanged when:
    - The experiment has no ``conditions:`` block (backward-compat).
    - ``user_sp["condition"]`` doesn't match any declared condition name
      (bare-label behavior preserved).
    - ``user_sp`` has no ``"condition"`` key at all.

    The returned dict is a shallow copy of ``user_sp`` in the pass-through
    cases; a merged new dict otherwise. Caller is free to mutate.
    """
    try:
        exp = load_experiment(experiment_id, kb_root=kb_root)
    except ArtifactNotFoundError:
        # Experiment not on disk — resolution is a no-op. The downstream
        # hook (enforce_hef_chain) will complain if this was meant to be
        # a real run; for tests that fake things, this keeps us permissive.
        return dict(user_sp)

    conditions = exp.metadata.get("conditions") or {}
    if not isinstance(conditions, dict):
        return dict(user_sp)

    condition_name = user_sp.get("condition")
    if condition_name is None or condition_name not in conditions:
        return dict(user_sp)

    block = conditions[condition_name]
    if not isinstance(block, dict):
        # Malformed conditions block — validator catches this; we degrade
        # to the bare-label path so a bad frontmatter doesn't brick queueing.
        return dict(user_sp)

    merged: dict[str, Any] = {**block, **user_sp}
    return merged


# ---------------------------------------------------------------------------
# Runner command templating
# ---------------------------------------------------------------------------


_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
"""Matches ``{key}`` only — not ``${key}`` or ``$key``, so shell vars
pass through untouched."""


def render_runner_command(
    template: str, sp: dict[str, Any], job_id: str
) -> str:
    """Substitute ``{key}`` / ``{sp_json}`` / ``{job_id}`` against ``sp``.

    - ``{key}`` where ``key`` is any sp field → ``str(sp[key])``.
    - ``{sp_json}`` → the full sp serialized as JSON (sorted keys).
    - ``{job_id}`` → the full 32-hex job id.
    - Unknown ``{xxx}`` placeholders → left as-is (so shell-quoted literals
      and stray braces don't raise). Shell vars (``$HOSTNAME``, ``${USER}``)
      are untouched because our regex requires a non-``$`` prefix.
    """
    sp_json_cache: str | None = None

    def _sub(match: re.Match[str]) -> str:
        nonlocal sp_json_cache
        key = match.group(1)
        if key == "sp_json":
            if sp_json_cache is None:
                # Compact separators: no whitespace inside. Critical when
                # the template splats {sp_json} into a shell argv — on
                # Windows cmd.exe, whitespace inside an unquoted (or
                # single-quoted, which cmd treats as literal) token
                # splits the JSON across multiple argv entries.
                sp_json_cache = json.dumps(
                    sp, sort_keys=True, separators=(",", ":"), default=str
                )
            return sp_json_cache
        if key == "job_id":
            return job_id
        if key in sp:
            return str(sp[key])
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template)


# ---------------------------------------------------------------------------
# Sweep expansion
# ---------------------------------------------------------------------------


def _parse_sweep_value(raw: str) -> list[Any]:
    """Parse one value side of a sweep spec: ``"a|b|c"`` or ``"0..3"``."""
    raw = raw.strip()
    # Integer range: "a..b" inclusive. Only simple integer ranges; no step.
    range_m = re.fullmatch(r"(-?\d+)\.\.(-?\d+)", raw)
    if range_m:
        lo, hi = int(range_m.group(1)), int(range_m.group(2))
        if hi < lo:
            raise SweepParseError(
                f"range {raw!r} has hi < lo; use {hi}..{lo} if you meant reverse"
            )
        return list(range(lo, hi + 1))
    # Enumerated strings separated by '|'. Try int-parsing each piece so
    # `seed=0|1|2` lands as ints, not strings.
    pieces = [p.strip() for p in raw.split("|") if p.strip()]
    if not pieces:
        raise SweepParseError(f"empty sweep value: {raw!r}")
    out: list[Any] = []
    for p in pieces:
        try:
            out.append(int(p))
        except ValueError:
            out.append(p)
    return out


def parse_sweep(spec: str) -> dict[str, list[Any]]:
    """Parse a sweep string into a dict ``{key: [values,...]}``.

    Grammar::

        spec    := clause ("," clause)*
        clause  := key "=" values
        values  := piece ("|" piece)*      # enumerated strings / ints
                 | integer ".." integer    # inclusive integer range

    Example::

        >>> parse_sweep("condition=full|classify_only, seed=0..3")
        {'condition': ['full', 'classify_only'], 'seed': [0, 1, 2, 3]}
    """
    result: dict[str, list[Any]] = {}
    for raw_clause in spec.split(","):
        clause = raw_clause.strip()
        if not clause:
            continue
        if "=" not in clause:
            raise SweepParseError(f"sweep clause missing '=': {clause!r}")
        key, _, raw_value = clause.partition("=")
        key = key.strip()
        if not key:
            raise SweepParseError(f"sweep clause has empty key: {clause!r}")
        result[key] = _parse_sweep_value(raw_value)
    return result


def _sweep_product(
    sweep: dict[str, list[Any]],
) -> Iterable[dict[str, Any]]:
    """Cartesian product across sweep keys → yields one sp dict per combo."""
    if not sweep:
        yield {}
        return
    keys = list(sweep.keys())
    value_lists = [sweep[k] for k in keys]
    for combo in itertools.product(*value_lists):
        yield {k: v for k, v in zip(keys, combo, strict=True)}


# ---------------------------------------------------------------------------
# Queue CRUD
# ---------------------------------------------------------------------------


def _kb_root(repo_root: str | Path | None) -> Path:
    return (Path(repo_root).resolve() if repo_root else find_repo_root()) / "kb"


def add_to_queue(
    *,
    experiment_id: str,
    statepoint: dict[str, Any] | None = None,
    hypothesis_id: str | None = None,
    sub_hypothesis_id: str | None = None,
    tag: str | None = None,
    runner_hint: str | None = None,
    runner_command_override: str | None = None,
    resolve_conditions: bool = True,
    repo_root: str | Path | None = None,
    include_commit: bool = True,
) -> signac.job.Job:
    """Register one pending run — signac job with ``status="queued"``.

    Creates (or reopens) a signac job via :func:`aexp.create_run`, then
    transitions it to ``queued`` and stamps ``job.doc["queue"]`` with the
    timestamp, tag, and any runner hints.

    If ``resolve_conditions`` (default) and ``statepoint["condition"]`` is
    a key in the linked experiment's ``conditions:`` frontmatter block,
    the block is merged into the statepoint *before* signac creates the
    job — so the resolved config is frozen to
    ``signac_statepoint.json``.

    ``runner_command_override`` pins a per-job command template that
    supersedes the experiment's ``runner_command`` frontmatter. Rare;
    useful for one-off tweaks.
    """
    user_sp = dict(statepoint or {})
    if resolve_conditions:
        kb = _kb_root(repo_root)
        user_sp = resolve_sp(experiment_id, user_sp, kb_root=kb)

    job = create_run(
        experiment_id=experiment_id,
        statepoint=user_sp,
        hypothesis_id=hypothesis_id,
        sub_hypothesis_id=sub_hypothesis_id,
        repo_root=repo_root,
        include_commit=include_commit,
        # We already resolved; tell create_run to skip its own pass so we
        # don't double-resolve (which would be idempotent, but is wasteful).
        resolve_conditions=False,
    )
    queue_doc: dict[str, Any] = {"queued_at": iso_utc_now()}
    if tag is not None:
        queue_doc["tag"] = tag
    if runner_hint is not None:
        queue_doc["runner_hint"] = runner_hint
    if runner_command_override is not None:
        queue_doc["runner_command_override"] = runner_command_override
    job.doc["queue"] = queue_doc
    job.doc["status"] = "queued"
    return job


def add_many_to_queue(
    *,
    experiment_id: str,
    base_sp: dict[str, Any] | None = None,
    sweep: dict[str, list[Any]] | None = None,
    hypothesis_id: str | None = None,
    sub_hypothesis_id: str | None = None,
    tag: str | None = None,
    runner_hint: str | None = None,
    resolve_conditions: bool = True,
    repo_root: str | Path | None = None,
    include_commit: bool = True,
) -> list[signac.job.Job]:
    """Bulk :func:`add_to_queue` via Cartesian product over ``sweep``.

    ``base_sp`` keys are applied to every job; ``sweep`` keys vary across
    jobs. ``base_sp`` and ``sweep`` must not overlap — pass the key in
    one or the other.
    """
    base = dict(base_sp or {})
    sweep = dict(sweep or {})
    overlap = set(base) & set(sweep)
    if overlap:
        raise ValueError(
            f"base_sp and sweep share keys: {sorted(overlap)}; "
            "put each key in exactly one"
        )
    jobs: list[signac.job.Job] = []
    for combo in _sweep_product(sweep):
        job_sp = {**base, **combo}
        jobs.append(
            add_to_queue(
                experiment_id=experiment_id,
                statepoint=job_sp,
                hypothesis_id=hypothesis_id,
                sub_hypothesis_id=sub_hypothesis_id,
                tag=tag,
                runner_hint=runner_hint,
                resolve_conditions=resolve_conditions,
                repo_root=repo_root,
                include_commit=include_commit,
            )
        )
    return jobs


def _job_to_queue_entry(job: signac.job.Job) -> QueueEntry:
    q = dict(job.doc.get("queue") or {})
    limina = dict(job.doc.get("limina") or {})
    return QueueEntry(
        job_id=job.id,
        experiment_id=limina.get("experiment_id") or job.sp.get("experiment_id"),
        hypothesis_id=limina.get("hypothesis_id") or job.sp.get("hypothesis_id"),
        status=job.doc.get("status"),
        tag=q.get("tag"),
        queued_at=q.get("queued_at"),
        sp=dict(job.sp),
        last_error=q.get("last_error"),
    )


_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "failed", "abandoned"})


def list_queue(
    *,
    experiment_id: str | None = None,
    tag: str | None = None,
    include_terminal: bool = False,
    repo_root: str | Path | None = None,
) -> list[QueueEntry]:
    """Return queue entries. Only jobs whose ``doc["queue"]`` is set.

    By default, jobs in a terminal status (``complete`` / ``failed`` /
    ``abandoned``) are hidden — queue listing is for *pending* work.
    Pass ``include_terminal=True`` to see historical queue entries too
    (useful when debugging).
    """
    jobs = find_runs(
        experiment_id=experiment_id,
        repo_root=repo_root,
    )
    out: list[QueueEntry] = []
    for job in jobs:
        q = job.doc.get("queue")
        if not q:
            continue
        if tag is not None and q.get("tag") != tag:
            continue
        status = job.doc.get("status")
        if not include_terminal and status in _TERMINAL_STATUSES:
            continue
        out.append(_job_to_queue_entry(job))
    # Stable order: queued_at ascending, then job_id.
    out.sort(key=lambda e: (e.queued_at or "", e.job_id))
    return out


def remove_from_queue(
    job_id: str, *, repo_root: str | Path | None = None
) -> signac.job.Job:
    """Mark a queued job ``abandoned`` without executing it.

    The signac workspace and ``job.doc["queue"]`` history are left intact
    so you can inspect what was abandoned later. Re-queuing requires
    :func:`add_to_queue` with the same sp (which will find the existing
    workspace and re-stamp ``status="queued"``).
    """
    job = open_run(job_id, repo_root=repo_root)
    mark_status(job, "abandoned")
    return job


def clear_queue(
    *,
    experiment_id: str | None = None,
    tag: str | None = None,
    repo_root: str | Path | None = None,
) -> list[str]:
    """Bulk-abandon every entry matching the filter. Returns job ids."""
    entries = list_queue(
        experiment_id=experiment_id, tag=tag, repo_root=repo_root
    )
    abandoned: list[str] = []
    for e in entries:
        remove_from_queue(e.job_id, repo_root=repo_root)
        abandoned.append(e.job_id)
    return abandoned


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


RunnerKind = Literal["shell", "slurm", "manual"]


def _format_slurm_directives(slurm_kwargs: dict[str, Any] | None) -> list[str]:
    if not slurm_kwargs:
        return []
    lines: list[str] = []
    extra = slurm_kwargs.pop("extra", None) if isinstance(slurm_kwargs, dict) else None
    for k, v in slurm_kwargs.items():
        # Normalize keys: time → --time, mem → --mem, gpus → --gpus, etc.
        flag = k.replace("_", "-")
        lines.append(f"#SBATCH --{flag}={v}")
    if extra:
        # Free-form string appended verbatim; split on newline so multiple
        # #SBATCH lines can be passed as one blob.
        for line in str(extra).splitlines():
            line = line.strip()
            if line:
                lines.append(line)
    return lines


def _emit_shell(job_ids: list[str], tag: str | None) -> str:
    lines = [
        "#!/usr/bin/env bash",
        f"# Generated by `aexp queue materialize` at {iso_utc_now()}",
        f"# {len(job_ids)} queued job(s)"
        + (f" under tag={tag}" if tag else ""),
        "set -e",
        'cd "$(dirname "$0")"',
        "",
    ]
    for jid in job_ids:
        lines.append(f"aexp run-queued {jid}")
    return "\n".join(lines) + "\n"


def _emit_slurm(
    job_ids: list[str], tag: str | None, slurm_kwargs: dict[str, Any] | None
) -> str:
    n = len(job_ids)
    if n == 0:
        raise ValueError("cannot emit slurm script for zero jobs")
    job_name = f"aexp-queue{'-' + tag if tag else ''}"
    header = [
        "#!/usr/bin/env bash",
        f"# Generated by `aexp queue materialize` at {iso_utc_now()}",
        f"# {n} queued job(s)"
        + (f" under tag={tag}" if tag else ""),
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --array=0-{n - 1}",
        "#SBATCH --output=logs/aexp-%A-%a.out",
        "#SBATCH --error=logs/aexp-%A-%a.err",
    ]
    header.extend(_format_slurm_directives(dict(slurm_kwargs or {})))
    header.extend(["", "jobs=("])
    for jid in job_ids:
        header.append(f"  {jid}")
    header.extend(
        [
            ")",
            "",
            'mkdir -p logs',
            'exec aexp run-queued "${jobs[$SLURM_ARRAY_TASK_ID]}"',
            "",
        ]
    )
    return "\n".join(header)


def _emit_manual(job_ids: list[str], tag: str | None) -> str:
    lines = [
        f"# Generated by `aexp queue materialize` at {iso_utc_now()}",
        f"# {len(job_ids)} queued job(s)"
        + (f" under tag={tag}" if tag else ""),
        "# Copy the lines below into your runner of choice:",
        "",
    ]
    for jid in job_ids:
        lines.append(f"aexp run-queued {jid}")
    return "\n".join(lines) + "\n"


def materialize_queue(
    *,
    runner: RunnerKind = "shell",
    output_path: str | Path = "run_queue.sh",
    experiment_id: str | None = None,
    tag: str | None = None,
    slurm_kwargs: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> MaterializeResult:
    """Emit a runner script covering every matching queue entry.

    Idempotent — re-running with the same filter overwrites the output
    file. ``run-queued`` itself is the thing that's idempotent at execution
    time (skips jobs already in a terminal status).
    """
    entries = list_queue(
        experiment_id=experiment_id, tag=tag, repo_root=repo_root
    )
    job_ids = [e.job_id for e in entries]
    if runner == "shell":
        body = _emit_shell(job_ids, tag)
    elif runner == "slurm":
        if not job_ids:
            raise ValueError(
                "no queued jobs match the filter; nothing to materialize"
            )
        body = _emit_slurm(job_ids, tag, slurm_kwargs)
    elif runner == "manual":
        body = _emit_manual(job_ids, tag)
    else:
        raise ValueError(
            f"unknown runner {runner!r}; expected shell|slurm|manual"
        )
    out = Path(output_path)
    atomic_write(out, body)
    return MaterializeResult(
        output_path=str(out.resolve()),
        runner=runner,
        num_jobs=len(job_ids),
        job_ids=job_ids,
    )


# ---------------------------------------------------------------------------
# run_queued — the runner-side entry point
# ---------------------------------------------------------------------------


def _resolve_runner_template(
    job: signac.job.Job, *, kb_root: Path
) -> str:
    """Find the command template to render: per-job override or experiment default."""
    q = dict(job.doc.get("queue") or {})
    override = q.get("runner_command_override")
    if override:
        return str(override)
    limina = dict(job.doc.get("limina") or {})
    exp_id = limina.get("experiment_id") or job.sp.get("experiment_id")
    if not exp_id:
        raise RunnerCommandMissing(
            f"job {job.id} has no experiment_id; cannot resolve runner_command"
        )
    try:
        exp = load_experiment(exp_id, kb_root=kb_root)
    except ArtifactNotFoundError as exc:
        raise RunnerCommandMissing(
            f"experiment {exp_id} not found on disk under {kb_root}"
        ) from exc
    template = (exp.metadata.get("runner_command") or "").strip()
    if not template:
        raise RunnerCommandMissing(
            f"experiment {exp_id} has no runner_command in frontmatter; "
            "add one to E###'s frontmatter or set "
            "job.doc['queue']['runner_command_override']"
        )
    return template


def run_queued(
    job_id: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    repo_root: str | Path | None = None,
) -> int:
    """Execute one queued job. Returns subprocess returncode (or 0 for skip/dry).

    Behavior:

    - If ``job.doc["status"]`` is ``complete`` / ``failed`` / ``abandoned``
      and ``force=False``: print a skip message and return 0. Idempotent
      re-execution of materialized scripts.
    - Otherwise: render the experiment's ``runner_command`` template (or
      the per-job override) against the job's resolved sp, invoke via
      ``subprocess.run(..., shell=True)`` inside :func:`run_lifecycle` so
      status transitions ``queued`` → ``running`` → ``complete``/``failed``
      automatically.
    - On non-zero exit, stderr tail (last 2KB) is captured into
      ``job.doc["queue"]["last_error"]`` before ``run_lifecycle`` marks
      the job failed.

    The subprocess inherits env plus ``AEXP_JOB_ID`` and
    ``AEXP_JOB_WORKSPACE`` so training scripts can re-open their own job
    or find their own workspace without argument threading.
    """
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    job = open_run(job_id, repo_root=root)
    status = job.doc.get("status")
    if status in _TERMINAL_STATUSES and not force:
        print(
            f"skipping {job_id[:8]}: already {status} (use --force to re-run)"
        )
        return 0

    kb = root / "kb"
    template = _resolve_runner_template(job, kb_root=kb)
    command = render_runner_command(template, dict(job.sp), job.id)

    if dry_run:
        print(command)
        return 0

    env = {
        **os.environ,
        "AEXP_JOB_ID": job.id,
        "AEXP_JOB_WORKSPACE": str(Path(job.path).resolve()),
    }

    with run_lifecycle(job):
        proc = subprocess.run(
            command,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        # Stream captured output so slurm logs aren't empty.
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            # Enrich job.doc with error context before run_lifecycle catches.
            existing = dict(job.doc.get("queue") or {})
            existing["last_error"] = {
                "returncode": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-2048:],
                "failed_at": iso_utc_now(),
            }
            job.doc["queue"] = existing
            raise SubprocessFailed(
                f"runner exited with code {proc.returncode}: "
                f"{(proc.stderr or '')[-512:]}"
            )
    return proc.returncode


__all__ = [
    "MaterializeResult",
    "QueueEntry",
    "RunnerCommandMissing",
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
    "run_queued",
]
