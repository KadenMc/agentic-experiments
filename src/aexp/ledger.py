"""Universal cross-machine ledger — Phase 2 of the cross-machine-ledger plan.

A *ledger entry* is a sanitized projection of a terminal-state signac job:
frozen statepoint, run-link, tracker URLs (NOT raw event log), code commit,
terminal timestamp. Stored at ``.aexp/ledger/<job_id>.json`` and committed
to git. Every machine that pulls the repo sees the same ledger, so finding
citations resolve consistently everywhere.

The ledger is a *projection*, not a copy. Per-machine debris — absolute
paths in ``tracker_log/events.jsonl``, wandb offline run directories, user
artifacts — stays in the gitignored ``.runs/workspace/<id>/``. The ledger
entry's tracker block has *pointers* (URL, run_id) but not the events.

Promotion mechanism:

- **Auto**: hook in :func:`aexp.runs.mark_status` fires on terminal
  transition (complete/failed/abandoned/stopped) and calls
  :func:`promote_to_ledger`. Wrapped in try/except so a promotion failure
  never crashes the run lifecycle.
- **Manual**: ``aexp ledger promote <id>`` for cases where the hook didn't
  fire (rare — out-of-band status writes, pre-Phase-2 runs).
- **Backfill**: ``aexp ledger backfill`` walks the local run store and
  promotes every terminal-state job not yet in ``.aexp/ledger/``. This is
  the migration tool — every machine that has terminal-state runs runs it
  once after upgrading to 0.6.

Idempotent. Re-promotion overwrites the file (statepoint is frozen so this
is safe; final docfields are append-mostly so re-promotion captures any
retroactive ``aexp link`` corrections, modulo the ``promoted_at`` timestamp
which always reflects the most recent write).

Sanitization is *allowlist-based*: explicit fields go into the projection;
nothing else does. Adding a new field to the projection requires editing
this module. The denylist alternative (drop fields ending in ``_path``)
fails-open under typos and surprises everybody downstream when a future
docfield gets accidentally leaked.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from aexp.runs import TERMINAL_STATUSES, get_run_store
from aexp.schema import iso_utc_now, read_run_link
from aexp.utils.atomic import atomic_write
from aexp.utils.paths import find_repo_root, read_machine_label

LEDGER_DIR_REL = Path(".aexp") / "ledger"
SCHEMA_VERSION = 1


class LedgerEntry(TypedDict, total=False):
    """Shape of one ``.aexp/ledger/<job_id>.json`` file."""

    schema_version: int
    job_id: str
    statepoint: dict[str, Any]
    run_link: dict[str, Any]
    status: str
    started_at: str
    ended_at: str
    wallclock_s: float
    tracker: dict[str, Any]
    code_commit: str
    code_dirty: bool
    registered_machine: str
    promoted_at: str


def _tracker_projection(doc: Any) -> dict[str, Any]:
    """Project the doc's tracker binding into the ledger's pointer-only form.

    Drops everything except (backend, run_id, url, group, project) — the
    fields a downstream reader actually needs to navigate to the run in
    the tracking backend. Specifically excludes any embedded init_kwargs
    or full config dict (those contain absolute workspace paths via the
    `"dir"` field — see ``trackers/base.py:185``).
    """
    raw = doc.get("tracker")
    if raw is None:
        return {}
    # signac wraps nested dicts in synced_collections types that aren't
    # strict `dict` subclasses; check for Mapping-shape duck-typing
    # instead. Convert to a plain dict before reading keys to also strip
    # any in-memory synced overhead.
    try:
        raw_dict = dict(raw)
    except (TypeError, ValueError):
        return {}
    keep: dict[str, Any] = {}
    for k in ("backend", "run_id", "url", "group", "project"):
        v = raw_dict.get(k)
        if v is not None:
            keep[k] = v
    return keep


def _statepoint_projection(sp: Any) -> dict[str, Any]:
    """Project the signac statepoint into a plain dict.

    Statepoints are frozen at create_run time and don't contain absolute
    paths in the aexp scheme — they're typed parameters (experiment_id,
    condition, seeds, etc.). We pass them through unchanged but copy so
    a downstream mutation doesn't bleed back into the in-memory job.
    """
    if hasattr(sp, "items"):
        return {k: v for k, v in sp.items()}
    if isinstance(sp, dict):
        return dict(sp)
    return {}


def project_to_ledger_entry(
    job: Any,
    *,
    machine_label: str | None = None,
) -> LedgerEntry:
    """Build the sanitized ledger entry for a terminal-state ``job``.

    Parameters
    ----------
    job : signac.job.Job
        Must be in a terminal status. Non-terminal jobs raise ValueError
        — promotion is for finished work only.
    machine_label : str | None
        Override for the ``registered_machine`` field. Defaults to
        whatever :func:`aexp.utils.paths.read_machine_label` returns
        for the job's project (typically the install's machine_label).
    """
    status = job.doc.get("status")
    if status not in TERMINAL_STATUSES:
        raise ValueError(
            f"refusing to promote job {job.id}: status={status!r} is not terminal "
            f"(expected one of {TERMINAL_STATUSES})"
        )

    entry: LedgerEntry = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job.id,
        "statepoint": _statepoint_projection(job.sp),
        "status": str(status),
        "promoted_at": iso_utc_now(),
    }

    link = read_run_link(job.doc)
    if link:
        entry["run_link"] = dict(link)

    for k in ("started_at", "ended_at"):
        v = job.doc.get(k)
        if v is not None:
            entry[k] = str(v)  # type: ignore[literal-required]

    wallclock = job.doc.get("wallclock_s")
    if isinstance(wallclock, (int, float)):
        entry["wallclock_s"] = float(wallclock)

    tracker = _tracker_projection(job.doc)
    if tracker:
        entry["tracker"] = tracker

    sp = entry["statepoint"]
    if "code_commit" in sp:
        entry["code_commit"] = str(sp["code_commit"])
    if "code_dirty" in sp:
        entry["code_dirty"] = bool(sp["code_dirty"])

    if machine_label is None:
        # Resolve from the project workspace's repo. We can't pass
        # repo_root through mark_status' hook chain cleanly, so derive
        # it from the job's project path (the parent of `.runs/`).
        try:
            project_path = Path(job._project.path)  # type: ignore[attr-defined]
            repo_root = project_path.parent
            machine_label = read_machine_label(repo_root)
        except Exception:
            machine_label = "unknown"
    entry["registered_machine"] = machine_label

    return entry


def promote_to_ledger(
    job: Any,
    *,
    repo_root: str | Path | None = None,
    machine_label: str | None = None,
) -> Path:
    """Promote a terminal-state ``job`` to ``.aexp/ledger/<job_id>.json``.

    Idempotent — re-promotion overwrites the file. Safe to call on a
    job that's already been promoted (the projection rebuild captures
    any new content like a recent ``aexp link`` re-stamping).

    Returns the absolute path of the written ledger entry.
    """
    if repo_root is None:
        try:
            project_path = Path(job._project.path)  # type: ignore[attr-defined]
            repo_root = project_path.parent
        except Exception:
            repo_root = find_repo_root()
    root = Path(repo_root).resolve()
    entry = project_to_ledger_entry(job, machine_label=machine_label)
    target = root / LEDGER_DIR_REL / f"{job.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(entry, indent=2, sort_keys=True) + "\n")
    return target


def ledger_path(repo_root: str | Path, job_id: str) -> Path:
    """Return the canonical ledger-entry path for ``job_id`` in ``repo_root``."""
    return Path(repo_root) / LEDGER_DIR_REL / f"{job_id}.json"


def load_ledger_entry(repo_root: str | Path, job_id: str) -> LedgerEntry | None:
    """Read a ledger entry by job_id. Returns None if missing or malformed."""
    path = ledger_path(repo_root, job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data  # type: ignore[return-value]


def list_ledger_job_ids(repo_root: str | Path) -> set[str]:
    """Return the set of job_ids currently in ``.aexp/ledger/``.

    The validator uses this as the cross-machine equivalent of
    ``known_job_ids = set(j.id for j in project)`` — once the ledger
    is the source of truth, a citation resolves iff the ledger entry
    file exists.
    """
    ledger_dir = Path(repo_root) / LEDGER_DIR_REL
    if not ledger_dir.is_dir():
        return set()
    return {p.stem for p in ledger_dir.glob("*.json")}


def backfill_ledger(
    repo_root: str | Path | None = None,
    *,
    machine_label: str | None = None,
    overwrite: bool = False,
) -> tuple[int, int]:
    """Walk the local run store and promote every terminal-state job.

    This is the Phase 2 migration tool. Every machine that has
    terminal-state runs runs this once after upgrading aexp; the
    resulting `.aexp/ledger/<id>.json` files get committed and pulled by
    other machines so the validator resolves citations universally.

    Returns ``(promoted, skipped_already_present)``.

    Parameters
    ----------
    repo_root : str | Path | None
        Consumer repo root. Defaults to ``find_repo_root()``.
    machine_label : str | None
        Tag every backfilled entry with this label. Defaults to
        ``read_machine_label(repo_root)``.
    overwrite : bool
        If True, re-promote even already-present entries. Useful after
        bumping the schema version or after a bulk ``aexp link`` fix.
    """
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    promoted = 0
    skipped = 0
    try:
        project = get_run_store(root)
    except Exception:
        return (0, 0)

    existing = list_ledger_job_ids(root)
    for job in project:
        if job.doc.get("status") not in TERMINAL_STATUSES:
            continue
        if job.id in existing and not overwrite:
            skipped += 1
            continue
        try:
            promote_to_ledger(job, repo_root=root, machine_label=machine_label)
            promoted += 1
        except Exception:
            # Don't let one bad job kill the whole backfill. The caller
            # can re-run with --overwrite later after investigating.
            continue
    return (promoted, skipped)


__all__ = [
    "LEDGER_DIR_REL",
    "SCHEMA_VERSION",
    "LedgerEntry",
    "backfill_ledger",
    "ledger_path",
    "list_ledger_job_ids",
    "load_ledger_entry",
    "project_to_ledger_entry",
    "promote_to_ledger",
]
