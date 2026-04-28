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

import errno
import itertools
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import signac

from aexp.limina_io import ArtifactNotFoundError, load_experiment
from aexp.runs import (
    create_run,
    find_runs,
    mark_status,
    open_run,
    run_lifecycle,
)
from aexp.schema import MaterializeResult, QueueEntry, iso_utc_now
from aexp.utils.atomic import atomic_write
from aexp.utils.git import get_dirty_diff_summary
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


class StopJobError(RuntimeError):
    """Raised by :func:`stop_queued` when stopping a job can't proceed.

    Distinguishes user-facing stop failures (host mismatch, kill refused,
    etc.) from generic ``RuntimeError`` so the CLI can surface them with
    actionable messages instead of a stack trace.
    """


# How many lines of subprocess stdout/stderr to retain for ``last_error``
# forensics. ~200 lines × ~80 chars/line ≈ 16 KB ceiling — well above the
# original 2 KB byte cap, which routinely truncated useful stack traces
# mid-sentence. Tail is rendered as the last ~2 KB of bytes so log-storage
# behavior matches the previous contract for short outputs.
_OUTPUT_TAIL_LINES: int = 200
_OUTPUT_TAIL_BYTES: int = 2048


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
    """Substitute ``{key}`` / ``{sp_json}`` / ``{sp_json_shell}`` /
    ``{job_id}`` against ``sp``.

    - ``{key}`` where ``key`` is any sp field → ``str(sp[key])``.
    - ``{sp_json}`` → the full sp serialized as JSON (sorted keys, compact
      separators). **Not shell-escaped.** If you wrap it in shell quotes
      (``'{sp_json}'``) and any sp value contains the same quote
      character, your runner will be invoked with broken argv. Use
      ``{sp_json_shell}`` for any shell-quoted context.
    - ``{sp_json_shell}`` → ``shlex.quote(<json>)``. POSIX-safe shell
      escaping that you should drop into the template *unquoted*::

          # CORRECT — drop in unquoted; shlex adds the quoting itself:
          runner_command: "python -m foo {sp_json_shell}"

          # WRONG — double-quoting; will result in single-quoted nest:
          runner_command: "python -m foo '{sp_json_shell}'"

      Windows cmd.exe doesn't honor POSIX single-quote shell rules; the
      cluster (Linux) is unaffected. Windows-local users should read
      ``signac_statepoint.json`` directly from ``$AEXP_JOB_WORKSPACE``
      rather than relying on argv passing.
    - ``{job_id}`` → the full 32-hex job id.
    - Unknown ``{xxx}`` placeholders → left as-is (so shell-quoted literals
      and stray braces don't raise). Shell vars (``$HOSTNAME``, ``${USER}``)
      are untouched because our regex requires a non-``$`` prefix.
    """
    sp_json_cache: str | None = None
    sp_json_shell_cache: str | None = None

    def _sub(match: re.Match[str]) -> str:
        nonlocal sp_json_cache, sp_json_shell_cache
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
        if key == "sp_json_shell":
            if sp_json_shell_cache is None:
                payload = json.dumps(
                    sp, sort_keys=True, separators=(",", ":"), default=str
                )
                # shlex.quote uses POSIX shell rules — single-quotes the
                # whole blob and escapes any embedded single-quotes via
                # the canonical ''\\'''-trick. Safe on bash/sh/zsh; the
                # docstring warns about Windows cmd.exe.
                sp_json_shell_cache = shlex.quote(payload)
            return sp_json_shell_cache
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


# Provenance keys that participate in signac's content-addressed sp hash
# but do *not* affect experiment identity. Two queue entries that differ
# only on these keys are logical duplicates (the second one was created
# because the user committed code in between, not because the
# experiment-level intent changed).
_PROVENANCE_SP_KEYS: frozenset[str] = frozenset({"code_commit", "code_dirty"})


def _sp_modulo_provenance(sp: dict[str, Any]) -> dict[str, Any]:
    """Drop provenance-only sp keys so two entries can be compared by intent."""
    return {k: v for k, v in sp.items() if k not in _PROVENANCE_SP_KEYS}


class DuplicatePendingJobWarning(UserWarning):
    """Emitted by :func:`add_to_queue` when a pending entry already matches
    the would-be-new sp modulo provenance fields.

    Captures the existing job id so callers can decide what to do
    (`-W error` to fail the queue add, default behavior to keep going).
    """


def _find_pending_dup(
    *,
    experiment_id: str,
    hypothesis_id: str | None,
    sub_hypothesis_id: str | None,
    target_sp: dict[str, Any],
    tag: str | None,
    repo_root: str | Path | None,
) -> signac.job.Job | None:
    """Return the first pending queued entry that matches ``target_sp``
    modulo provenance, scoped to ``(experiment_id, tag)``.

    Tag scope is deliberate: an operator who tags two queueings differently
    (e.g. ``2026-04-26-morning`` vs ``2026-04-26-evening``) is signaling a
    real partition of work, not asking aexp to merge them. Tag-less and
    tag-less always match each other; tag=X matches tag=X only.

    The comparison reconstructs the *would-be* sp on the target side
    (user_sp + auto-injected ``experiment_id`` + optional hypothesis ids),
    minus provenance, so it lines up with what ``_build_statepoint`` will
    put on disk when ``create_run`` is invoked. Without this reconstruction
    the target lacks ``experiment_id`` and the comparison spuriously
    misses every existing entry.
    """
    # Mirror what create_run / _build_statepoint will inject so the target
    # sp matches the on-disk shape of existing entries.
    target_full: dict[str, Any] = {**target_sp, "experiment_id": experiment_id}
    if hypothesis_id is not None:
        target_full["hypothesis_id"] = hypothesis_id
    if sub_hypothesis_id is not None:
        target_full["sub_hypothesis_id"] = sub_hypothesis_id
    target_modulo = _sp_modulo_provenance(target_full)

    existing_entries = list_queue(
        experiment_id=experiment_id, tag=tag, repo_root=repo_root
    )
    for entry in existing_entries:
        if entry.status != "queued":
            # list_queue already excludes terminal statuses by default;
            # this guards the rare in-flight ``running`` row.
            continue
        if _sp_modulo_provenance(entry.sp) == target_modulo:
            # Re-open the actual job for the caller to inspect / return.
            return open_run(entry.job_id, repo_root=repo_root)
    return None


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
    allow_dup_on_recommit: bool = False,
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

    Recommit deduplication
    ----------------------
    By default (``allow_dup_on_recommit=False``), if a pending queue
    entry already exists for the same ``experiment_id`` + ``tag`` whose
    sp matches the about-to-be-added sp *modulo* the provenance keys
    ``code_commit`` and ``code_dirty``, ``add_to_queue`` returns the
    existing job and emits a :class:`DuplicatePendingJobWarning` instead
    of creating a near-duplicate signac job. This catches the common
    footgun of "queue, commit a docstring fix, queue again — now you
    have N+N pending jobs that are functionally identical."

    Pass ``allow_dup_on_recommit=True`` (or ``--allow-dup-on-recommit``
    on the CLI) when the new code_commit *is* the point of the new
    entries — e.g. evaluating a fix in parallel with the pre-fix runs.
    Terminal-status entries (complete / failed / abandoned / stopped)
    are never deduped against; they're historical, and re-running an
    experiment after it completed is not a duplicate-by-mistake case.
    """
    import warnings

    user_sp = dict(statepoint or {})
    if resolve_conditions:
        kb = _kb_root(repo_root)
        user_sp = resolve_sp(experiment_id, user_sp, kb_root=kb)

    if not allow_dup_on_recommit:
        existing = _find_pending_dup(
            experiment_id=experiment_id,
            hypothesis_id=hypothesis_id,
            sub_hypothesis_id=sub_hypothesis_id,
            target_sp=user_sp,
            tag=tag,
            repo_root=repo_root,
        )
        if existing is not None:
            warnings.warn(
                (
                    f"queue add skipped: pending entry {existing.id[:8]} "
                    f"already matches sp modulo code_commit/code_dirty "
                    f"(experiment={experiment_id}"
                    f"{', tag=' + tag if tag else ''}). "
                    "Pass allow_dup_on_recommit=True (or "
                    "--allow-dup-on-recommit) to add the new entry anyway."
                ),
                DuplicatePendingJobWarning,
                stacklevel=2,
            )
            return existing

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

    # If the working tree was dirty when this job was queued, the bare
    # ``code_commit`` SHA isn't a precise reproducer — there are
    # uncommitted changes layered on top. Capture a structured summary
    # of what differed (stat + counts) so post-hoc forensics has a
    # fighting chance of reconstructing what was actually run.
    if include_commit and job.sp.get("code_dirty"):
        try:
            root = (
                Path(repo_root).resolve() if repo_root else find_repo_root()
            )
            queue_doc["code_diff_summary"] = get_dirty_diff_summary(root)
        except Exception:
            # Provenance capture is best-effort. Don't fail the queue
            # add because git is unavailable or the tree is in an odd
            # state — the queue functionality is more important than
            # the forensics field.
            pass

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
    allow_dup_on_recommit: bool = False,
) -> list[signac.job.Job]:
    """Bulk :func:`add_to_queue` via Cartesian product over ``sweep``.

    ``base_sp`` keys are applied to every job; ``sweep`` keys vary across
    jobs. ``base_sp`` and ``sweep`` must not overlap — pass the key in
    one or the other.

    Recommit deduplication is applied per-combo via :func:`add_to_queue`.
    A sweep that overlaps an existing pending grid will return the
    existing pending jobs (one warning per matched combo) instead of
    materializing a parallel set of dups; pass
    ``allow_dup_on_recommit=True`` to override.
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
                allow_dup_on_recommit=allow_dup_on_recommit,
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


_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"complete", "failed", "abandoned", "stopped"}
)


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
    job_ids: list[str],
    tag: str | None,
    slurm_kwargs: dict[str, Any] | None,
    experiment_id: str | None,
) -> str:
    n = len(job_ids)
    if n == 0:
        raise ValueError("cannot emit slurm script for zero jobs")
    job_name = f"aexp-queue{'-' + tag if tag else ''}"

    # Build the filter args that `aexp queue run --index ...` will use.
    # Baking the filter (not the specific job ids) into the script lets
    # re-queueing after materialization still work — the array task
    # resolves the pending job at run-time, not materialize-time.
    filter_args: list[str] = []
    if tag is not None:
        filter_args.extend(["--tag", tag])
    if experiment_id is not None:
        filter_args.extend(["--experiment", experiment_id])
    filter_str = " ".join(filter_args)

    lines = [
        "#!/usr/bin/env bash",
        f"# Generated by `aexp queue materialize --runner slurm` at {iso_utc_now()}",
        f"# {n} queued job(s)"
        + (f" under tag={tag}" if tag else ""),
        "#",
        "# ─────────────────────────────────────────────────────────────",
        "# STARTER TEMPLATE — NOT A TURN-KEY SLURM SCRIPT.",
        "#",
        "# aexp has no visibility into your cluster's conventions:",
        "# partition, account, module loads, env activation, container",
        "# setup, institutional constraints. The #SBATCH block and the",
        "# site-setup commands below are placeholders you must fill in.",
        "#",
        "# The only aexp-specific line is the final `aexp queue run`",
        "# invocation. You can also skip this generated file entirely",
        "# and call that line from your own existing batch script.",
        "# ─────────────────────────────────────────────────────────────",
        "",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --array=0-{n - 1}",
        "#SBATCH --output=logs/aexp-%A-%a.out",
        "#SBATCH --error=logs/aexp-%A-%a.err",
    ]
    extra_directives = _format_slurm_directives(dict(slurm_kwargs or {}))
    if extra_directives:
        lines.extend(extra_directives)
    else:
        lines.extend(
            [
                "# TODO: add #SBATCH --partition=... --account=... --time=...",
                "# TODO: add #SBATCH --mem=... --gpus=... as your site requires.",
            ]
        )
    lines.extend(
        [
            "",
            "# Site-specific setup — uncomment and edit as needed:",
            "# module load cuda/12.1",
            "# source ~/miniconda3/bin/activate <env>",
            "# cd /path/to/this/repo",
            "",
            "mkdir -p logs",
            "",
            "# aexp resolves the pending-queue index → one specific job at run-time.",
            "# If you re-queue jobs between submission and execution, the array will",
            "# pick up whatever's pending under this filter at task-launch time.",
            f'exec aexp queue run {filter_str}'.rstrip() + ' --index "$SLURM_ARRAY_TASK_ID"',
            "",
        ]
    )
    return "\n".join(lines)


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
        body = _emit_slurm(job_ids, tag, slurm_kwargs, experiment_id)
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


def _proc_create_time(pid: int) -> float | None:
    """Best-effort process-start-time fingerprint for PID-recycle detection.

    Returns ``None`` when we can't read a stable identifier for the PID's
    start time. Callers treat ``None`` as "no fingerprint available; skip
    the recycle check" — same semantics as the rare race we accept on
    Windows / non-procfs platforms.

    Implementation:

    - Linux: read field 22 (``starttime``, in clock ticks since boot) from
      ``/proc/<pid>/stat``. Stable across the lifetime of the process and
      cheap to read; survives container-vs-host quirks because procfs is
      always the running pid namespace's view.
    - Other platforms: ``None``. ``psutil`` would give us this portably
      but adding a dep just for stop-job recycling is overkill — the
      cluster is Linux, which is where this matters.
    """
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
                # comm field can contain spaces in parentheses; rsplit
                # off the trailing fields to dodge that.
                content = fh.read()
        except (OSError, FileNotFoundError):
            return None
        # Strip up to the closing paren of comm, then split the rest.
        try:
            tail = content[content.rindex(")") + 2:]
            fields = tail.split()
            # starttime is the (22 - 2)th field after the comm split.
            return float(fields[19])
        except (ValueError, IndexError):
            return None
    return None


def _record_running_proc(
    job: signac.job.Job, *, pid: int, pgid: int | None, host: str
) -> None:
    """Stamp ``job.doc["queue"]["proc"]`` so :func:`stop_queued` can find us."""
    queue_doc = dict(job.doc.get("queue") or {})
    proc_info: dict[str, Any] = {
        "pid": pid,
        "host": host,
        "started_at": iso_utc_now(),
    }
    if pgid is not None:
        proc_info["pgid"] = pgid
    fingerprint = _proc_create_time(pid)
    if fingerprint is not None:
        proc_info["start_fingerprint"] = fingerprint
    queue_doc["proc"] = proc_info
    job.doc["queue"] = queue_doc


def _clear_running_proc(job: signac.job.Job) -> None:
    """Drop ``job.doc["queue"]["proc"]`` once the subprocess has exited.

    Leaving stale proc info in place is a footgun: an operator running
    ``aexp queue stop`` after the job exited would think they were
    targeting a live process. We blow away ``proc`` on every exit path
    so the only time it's present is between Popen-spawn and wait-return.
    """
    queue_doc = dict(job.doc.get("queue") or {})
    queue_doc.pop("proc", None)
    job.doc["queue"] = queue_doc


def _spawn_subprocess(
    command: str, *, env: dict[str, str], cwd: str
) -> tuple[subprocess.Popen[str], int | None]:
    """Spawn the runner subprocess in its own process group.

    Returns ``(proc, pgid)``. On POSIX, ``pgid`` is the new session/group id
    (``os.setsid`` makes the child the leader of a new session — which makes
    its pid equal to its pgid — so ``stop_queued`` can SIGTERM the whole
    tree with one ``os.killpg``). On Windows, we set
    ``CREATE_NEW_PROCESS_GROUP`` instead and return ``pgid=None``; the
    Windows kill path uses ``CTRL_BREAK_EVENT`` against the pid directly.
    """
    popen_kwargs: dict[str, Any] = {
        "shell": True,
        "env": env,
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        # Merge stderr into stdout so a single line-iteration loop preserves
        # interleaving order (which is what an interactive notebook user
        # actually wants — the runner's own stderr context lands next to
        # the stdout it's commenting on, not at the end of the run).
        "stderr": subprocess.STDOUT,
        "bufsize": 1,
        "text": True,
    }
    if sys.platform == "win32":
        # Windows: detach into its own process group so we can later send
        # CTRL_BREAK_EVENT. CREATE_NEW_PROCESS_GROUP=0x00000200.
        popen_kwargs["creationflags"] = 0x00000200
        proc = subprocess.Popen(command, **popen_kwargs)
        return proc, None
    # POSIX: start a new session via setsid so the runner + its descendants
    # share a pgid distinct from the parent. preexec_fn is documented as
    # not-thread-safe on the parent side; we call from the run_queued main
    # thread only, so this is fine.
    popen_kwargs["preexec_fn"] = os.setsid
    proc = subprocess.Popen(command, **popen_kwargs)
    return proc, proc.pid  # session-leader: pgid == pid


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
      / ``stopped`` and ``force=False``: print a skip message and return
      0. Idempotent re-execution of materialized scripts.
    - Otherwise: render the experiment's ``runner_command`` template (or
      the per-job override) against the job's resolved sp, invoke via
      ``subprocess.Popen(..., shell=True)`` inside :func:`run_lifecycle`
      with stdout/stderr streamed line-by-line to the parent's stdout
      so interactive consumers (notebooks, terminals) see live progress.
      Status transitions ``queued`` → ``running`` →
      ``complete`` / ``failed`` automatically.
    - The last ~200 lines of merged output are kept in a ring buffer and
      written into ``job.doc["queue"]["last_error"].stderr_tail`` (last
      ~2 KB, matching the prior contract) on non-zero exit.
    - During execution, ``job.doc["queue"]["proc"]`` records the
      subprocess pid / pgid / host / start fingerprint so
      :func:`stop_queued` can interrupt the run from another shell.

    The subprocess inherits env plus ``AEXP_JOB_ID`` and
    ``AEXP_JOB_WORKSPACE`` so training scripts can re-open their own job
    or find their own workspace without argument threading.

    Notes
    -----
    stderr is merged into stdout (``stderr=STDOUT``) before streaming, so
    the parent only sees one combined stream. This was a deliberate trade
    for the v0.2.1 fix: a single iteration loop preserves the interleave
    order that interactive users expect, at the cost of distinguishing
    streams in the ``last_error`` capture. If a future use case needs them
    separate, the right path is two ``selectors``-monitored pipes; the
    ring-buffer-tee structure here is the same.
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

    returncode = -1
    with run_lifecycle(job):
        proc, pgid = _spawn_subprocess(command, env=env, cwd=str(root))
        try:
            _record_running_proc(
                job, pid=proc.pid, pgid=pgid, host=socket.gethostname()
            )
            # Bounded ring buffer of recent lines for the failure-tail
            # capture path. We tail-truncate to ~2 KB at write time so the
            # signac doc doesn't bloat with megabytes of normal output.
            tail_buffer: deque[str] = deque(maxlen=_OUTPUT_TAIL_LINES)
            assert proc.stdout is not None  # bounded by Popen kwargs above
            for line in proc.stdout:
                # Stream live to the parent. Flush after every line so a
                # JupyterLab user sees output as it lands instead of after
                # cell completion. ``line`` already includes the trailing
                # newline; don't add another.
                sys.stdout.write(line)
                sys.stdout.flush()
                tail_buffer.append(line)
            proc.wait()
            returncode = proc.returncode
            if returncode != 0:
                tail_text = "".join(tail_buffer)[-_OUTPUT_TAIL_BYTES:]
                existing = dict(job.doc.get("queue") or {})
                # Don't clobber an operator_stop record: when ``aexp queue
                # stop`` killed the subprocess, it already wrote a
                # last_error with cause="operator_stop" to the doc.
                # Overwriting that here with a generic "subprocess
                # failed" record would bury the operator's intent in
                # post-hoc forensics. The status guard in run_lifecycle
                # similarly preserves "stopped" status.
                prior_cause = (
                    existing.get("last_error", {}).get("cause")
                    if isinstance(existing.get("last_error"), dict)
                    else None
                )
                if prior_cause != "operator_stop":
                    existing["last_error"] = {
                        "returncode": returncode,
                        "stderr_tail": tail_text,
                        "failed_at": iso_utc_now(),
                    }
                    job.doc["queue"] = existing
                # Still raise SubprocessFailed so run_lifecycle records
                # an exception exit (it just won't overwrite a stopped
                # status thanks to the preservation guard there).
                raise SubprocessFailed(
                    f"runner exited with code {returncode}: "
                    f"{tail_text[-512:]}"
                )
        finally:
            # Always drop the proc pointer so a downstream `queue stop` can't
            # be tricked into killing a recycled PID. Lives in `finally`
            # rather than after the wait so even an unexpected exception
            # path scrubs the ledger. run_lifecycle has its own try/except
            # that catches SubprocessFailed and marks status=failed; clearing
            # proc *before* that runs is fine because run_lifecycle only
            # writes status / wallclock_s, not queue.proc.
            _clear_running_proc(job)
    return returncode


def run_queue(
    *,
    experiment_id: str | None = None,
    tag: str | None = None,
    index: int | None = None,
    continue_on_failure: bool = False,
    force: bool = False,
    dry_run: bool = False,
    repo_root: str | Path | None = None,
) -> list[int]:
    """Execute queued jobs matching the filter — the inside-your-batch-script API.

    Designed to be invoked from whatever batch script (slurm job, qsub,
    bare bash, docker-compose run, ...) the user already has working for
    their site. aexp has no visibility into cluster conventions
    (partition, module loads, env activation, container setup); the
    user's script owns all that. ``aexp queue run`` just iterates the
    pending queue for the filter and calls :func:`run_queued` on each
    job, so aexp owns the iteration and status reconciliation while the
    user owns everything else.

    Two usage patterns:

    **Sequential (single-node)** — run every pending job in order::

        aexp queue run --tag overnight

    **Array-parallel** — pick one job by index, run only that one. Each
    slurm array task gets a distinct index, so tasks execute in parallel
    across nodes::

        #SBATCH --array=0-7
        aexp queue run --tag overnight --index "$SLURM_ARRAY_TASK_ID"

    Jobs within the filter are enumerated in the stable order
    ``list_queue`` returns (ascending ``queued_at``, then by job id), so
    ``--index N`` picks a deterministic job.

    Parameters
    ----------
    experiment_id, tag : str | None
        Filter passed through to ``list_queue``. Terminal-status jobs
        are excluded (the filter is pending-only).
    index : int | None
        If given, run only the Nth pending job. Out-of-range raises
        ``IndexError``. Intended for slurm array tasks.
    continue_on_failure : bool
        If ``True`` and multiple jobs are attempted, continue after a
        failure rather than raising. Only meaningful when ``index`` is
        ``None``. Failed returncodes are still included in the returned
        list; the caller can inspect them.
    force, dry_run : bool
        Forwarded to :func:`run_queued` per invocation.
    repo_root : str | Path | None
        Consumer repo root.

    Returns
    -------
    list[int]
        Subprocess returncodes, one per job attempted. For ``--index``
        mode, length 1. For whole-queue mode, length equals the number
        of pending jobs in the filter. Non-zero entries mean failure.

    Raises
    ------
    SubprocessFailed
        On the first failing job when ``continue_on_failure`` is False
        (default). ``run_lifecycle`` has already marked the job failed
        and captured the stderr tail.
    IndexError
        If ``index`` is out of range for the pending queue.
    """
    entries = list_queue(
        experiment_id=experiment_id, tag=tag, repo_root=repo_root
    )
    if not entries:
        return []

    if index is not None:
        if index < 0 or index >= len(entries):
            raise IndexError(
                f"--index {index} out of range for {len(entries)} pending job(s)"
            )
        targets = [entries[index]]
    else:
        targets = entries

    returncodes: list[int] = []
    for entry in targets:
        try:
            rc = run_queued(
                entry.job_id,
                force=force,
                dry_run=dry_run,
                repo_root=repo_root,
            )
        except SubprocessFailed:
            if continue_on_failure:
                returncodes.append(1)
                continue
            raise
        returncodes.append(rc)
    return returncodes


# ---------------------------------------------------------------------------
# stop_queued — interrupt a running job from another shell
# ---------------------------------------------------------------------------


def _send_stop_signal(
    pid: int, pgid: int | None, *, force: bool
) -> bool:
    """Send a graceful or forceful stop signal to the process (group).

    Parameters
    ----------
    pid : int
        Target process id.
    pgid : int | None
        Process group id on POSIX (sent the signal preferentially, so
        the runner's children die with it). Ignored on Windows.
    force : bool
        ``False``: graceful. POSIX → ``SIGTERM`` to pgid (or pid).
        Windows → ``CTRL_BREAK_EVENT`` to pid (best-effort; only delivers
        same-console — cross-shell invocations silently no-op, and that's
        a Win32 console-API limitation we can't work around without
        kernel-level injection. The force-kill escalation below is the
        safety net for cross-console use cases.)
        ``True``: forceful. POSIX → ``SIGKILL`` to pgid (or pid).
        Windows → ``taskkill /PID <pid> /F /T``: ``/F`` invokes
        ``TerminateProcess`` (works cross-console; doesn't depend on the
        console-attached signal API), ``/T`` walks the process tree so
        the inner ``python.exe`` running the runner command dies along
        with the ``cmd.exe`` shell wrapper that
        ``subprocess.Popen(shell=True)`` spawns.

    Returns
    -------
    bool
        ``True`` on successful signal/kill send, ``False`` if the target
        was already gone (POSIX ESRCH / Windows "process not found").
        Any other I/O failure propagates as ``OSError``.

    Why this signature instead of ``(pid, pgid, sig)``
    --------------------------------------------------
    The previous shape took a ``signal.Signals`` value and dispatched
    on ``sig == signal.SIGTERM``. That broke on Windows because
    ``signal.SIGKILL`` doesn't exist there: the escalation path
    fell back to ``signal.SIGTERM`` which the dispatch routed to
    ``CTRL_BREAK_EVENT``, never reaching ``taskkill``. Encoding
    *intent* (force=bool) instead of *signal value* removes that whole
    class of dispatch ambiguity — the force path can never be confused
    for the graceful path no matter which constants the platform
    happens to define.
    """
    try:
        if sys.platform == "win32":
            if not force:
                # Best-effort graceful interrupt; see docstring for why
                # this is expected to silently no-op cross-console.
                os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                return True
            # Forceful: tree-kill via taskkill. Capture stderr to
            # disambiguate "process already gone" (rc=128, "not found")
            # from real failures.
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return True
            stderr = (result.stderr or "").strip()
            # taskkill returns 128 / says "not found" for dead pids —
            # the cross-platform analog of ESRCH on POSIX.
            if (
                result.returncode == 128
                or "not found" in stderr.lower()
                or "no running instance" in stderr.lower()
            ):
                return False
            raise OSError(
                f"taskkill /PID {pid} /F /T exited "
                f"{result.returncode}: {stderr}"
            )
        # POSIX
        sig = signal.SIGKILL if force else signal.SIGTERM
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        raise


def _proc_alive(pid: int, pgid: int | None) -> bool:
    """Best-effort liveness probe.

    POSIX: signal 0 to the pgid (if known) or pid; ESRCH means dead.
    Windows: signal 0 to the pid.
    """
    try:
        if sys.platform == "win32":
            os.kill(pid, 0)
        elif pgid is not None:
            os.killpg(pgid, 0)
        else:
            os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def stop_queued(
    job_id: str,
    *,
    grace_s: float = 5.0,
    force: bool = False,
    repo_root: str | Path | None = None,
) -> int:
    """Interrupt a live :func:`run_queued` subprocess. Returns 0 on success.

    Reads the live-process pointer that ``run_queued`` writes into
    ``job.doc["queue"]["proc"]`` (pid + optional pgid + host +
    start-time fingerprint) and sends signals to interrupt the runner:

    1. Validate the host: refuses to send signals if the recorded host
       differs from this machine. (Job pids are local to the machine that
       spawned them; a cluster operator running ``stop`` from a different
       login node would otherwise be SIGKILLing a recycled pid on the
       wrong host.) Override is impossible by design — the right answer
       is to ssh / re-launch the verb on the recording host.
    2. PID-recycle guard: if a start-time fingerprint was recorded, compare
       it to the current PID's fingerprint. Mismatch ⇒ original process
       is gone; we transition status to ``stopped`` without sending any
       signals, so we never accidentally kill an unrelated process.
    3. Send ``SIGTERM`` to the process group (POSIX) /
       ``CTRL_BREAK_EVENT`` (Windows). Poll for exit up to ``grace_s``.
    4. If still alive, escalate to ``SIGKILL`` (POSIX) / ``SIGTERM``
       (Windows) and poll again briefly.
    5. Stamp ``last_error = {returncode, stderr_tail: "stopped via aexp
       queue stop", failed_at}`` and transition status to ``stopped``.

    Parameters
    ----------
    job_id : str
        Signac job id of a job currently being executed by ``run_queued``.
        Jobs that aren't running (no ``queue.proc``) end up as ``stopped``
        with ``last_error.returncode = 0`` and a note that no process was
        live — useful for cleaning up after an aexp parent process crash
        that left the status as ``running``.
    grace_s : float
        Seconds to wait between SIGTERM and SIGKILL escalation. Set to 0
        to skip the grace period; ``--force`` does the same on the CLI.
    force : bool
        If ``True``, skip SIGTERM and go straight to SIGKILL. Use only
        when a graceful shutdown is known not to work (the runner ignores
        SIGTERM, or you need the kernel to free GPU memory immediately).
    repo_root : str | Path | None

    Returns
    -------
    int
        0 — process killed / already dead / never recorded as live.
        Non-zero is currently never returned; failure modes (host
        mismatch, kill refused) raise :class:`StopJobError` instead.

    Raises
    ------
    StopJobError
        On host mismatch or unrecoverable kill failure.
    RunNotFound
        If the job id doesn't exist in the run store.
    """
    job = open_run(job_id, repo_root=repo_root)
    queue_doc = dict(job.doc.get("queue") or {})
    proc_info = queue_doc.get("proc")

    # Path A: no live process recorded. Either the job never ran, or it
    # finished cleanly (proc is cleared on every exit). Transition status
    # without signaling anything.
    if not proc_info:
        return _finalize_stopped(
            job, returncode=0, note="no live process recorded; status only"
        )

    pid = int(proc_info.get("pid", 0))
    pgid_raw = proc_info.get("pgid")
    pgid = int(pgid_raw) if pgid_raw is not None else None
    recorded_host = str(proc_info.get("host") or "")
    recorded_fp = proc_info.get("start_fingerprint")

    # Step 1: host guard. signals are local; refusing the wrong-host case
    # avoids the worst-case of SIGKILLing an unrelated process on a
    # recycled pid.
    this_host = socket.gethostname()
    if recorded_host and recorded_host != this_host:
        raise StopJobError(
            f"job {job_id[:8]} was started on host {recorded_host!r}; "
            f"this machine is {this_host!r}. signals don't cross hosts — "
            "ssh into the recording host and rerun `aexp queue stop "
            f"{job_id[:8]}` there."
        )

    # Step 2: PID-recycle guard. If we have a stable start-time fingerprint
    # and it doesn't match the current PID, the original process is gone
    # and the PID may belong to something completely unrelated.
    if recorded_fp is not None and pid > 0:
        current_fp = _proc_create_time(pid)
        if current_fp is not None and abs(current_fp - float(recorded_fp)) > 0.5:
            return _finalize_stopped(
                job,
                returncode=0,
                note=(
                    f"recorded pid {pid} was recycled; original process "
                    "is gone. status only."
                ),
            )

    # If the pid is already gone, we're done — just transition.
    if pid > 0 and not _proc_alive(pid, pgid):
        return _finalize_stopped(
            job, returncode=0, note=f"pid {pid} already exited; status only"
        )

    # Step 3: graceful stop (skipped if --force).
    sigterm_returncode = -int(signal.SIGTERM)
    if not force:
        sent = _send_stop_signal(pid, pgid, force=False)
        if not sent:
            return _finalize_stopped(
                job, returncode=0, note=f"pid {pid} vanished before SIGTERM"
            )
        # Poll for exit during the grace window.
        deadline = time.monotonic() + max(0.0, float(grace_s))
        while time.monotonic() < deadline:
            if not _proc_alive(pid, pgid):
                return _finalize_stopped(
                    job,
                    returncode=sigterm_returncode,
                    note="terminated cleanly via SIGTERM",
                )
            time.sleep(0.1)

    # Step 4: forceful kill (escalation after grace, or immediate when
    # ``force=True``). On POSIX this is SIGKILL via ``os.killpg``; on
    # Windows it's ``taskkill /F /T``. The forensics returncode we
    # record is -9 (SIGKILL on POSIX) by convention; Windows doesn't
    # define SIGKILL but the same value is conventional for "force-
    # killed" exit codes.
    sigkill_returncode = -int(getattr(signal, "SIGKILL", 9))
    sent = _send_stop_signal(pid, pgid, force=True)
    if not sent:
        # Might've died between the SIGTERM-escalate-deadline and now.
        return _finalize_stopped(
            job,
            returncode=sigterm_returncode if not force else 0,
            note=(
                "process exited between SIGTERM grace and SIGKILL "
                "(no force-kill needed)"
            ),
        )
    # Brief wait so the kernel actually reaps; if it's still listed as
    # alive after ~2s post-kill something is genuinely wrong (zombie,
    # uninterruptible sleep on dead I/O).
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _proc_alive(pid, pgid):
            break
        time.sleep(0.1)
    return _finalize_stopped(
        job,
        returncode=sigkill_returncode,
        note=("force-killed" if force else "escalated to force-kill"),
    )


def _finalize_stopped(
    job: signac.job.Job, *, returncode: int, note: str
) -> int:
    """Stamp ``last_error`` + transition to ``stopped`` and return 0.

    Centralized so every stop-path emits the same shape into ``job.doc``
    regardless of whether the kill needed SIGKILL escalation or the
    process was already dead.
    """
    queue_doc = dict(job.doc.get("queue") or {})
    queue_doc["last_error"] = {
        "returncode": returncode,
        "stderr_tail": f"stopped via `aexp queue stop`: {note}",
        "failed_at": iso_utc_now(),
        "cause": "operator_stop",
    }
    queue_doc.pop("proc", None)
    job.doc["queue"] = queue_doc
    mark_status(job, "stopped")
    return 0


__all__ = [
    "MaterializeResult",
    "QueueEntry",
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
]
