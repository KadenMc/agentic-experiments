"""Per-machine run index — Phase 1B of the cross-machine-ledger plan.

Each machine periodically dumps a JSON list of its terminal-state runs to
``.aexp/runs-index/<machine_label>.json``. The validator unions these index
files at validate time and emits ``finding.absent_run_citation`` (warning)
for citations that resolve in an index but not in the local run store —
distinguishing "lives on the cluster's ledger" from "broken" at validate
time without the user needing ``--strict-runs=warn``.

This is transitional infrastructure. Phase 2's universal ledger
(`aexp.ledger`) supersedes it; this module + the ``aexp runs-export-index``
verb stay through one minor-version deprecation window after Phase 2 ships,
then get removed. Phase 1B's index files double as the migration tool for
Phase 2's ``aexp ledger backfill``.

Schema of a single ``runs-index/<machine_label>.json``::

    {
      "schema_version": 1,
      "machine_label": "cluster",
      "exported_at": "2026-05-26T01:23:45Z",
      "entries": [
        {
          "job_id": "90d33bd...",
          "experiment_id": "E005",
          "condition": "uhn_from_ked__uhn",
          "status": "complete",
          "registered_at": "2026-05-14T18:22:01Z"
        },
        ...
      ]
    }

Only terminal-state jobs (``complete``/``failed``/``abandoned``/``stopped``)
are exported. Queued/running/created jobs are local mutable state and don't
belong in the cross-machine index.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from aexp.runs import TERMINAL_STATUSES, RunStoreNotInitialized, get_run_store
from aexp.schema import iso_utc_now, read_run_link
from aexp.utils.atomic import atomic_write
from aexp.utils.paths import find_repo_root, read_machine_label

INDEX_DIR_REL = Path(".aexp") / "runs-index"
SCHEMA_VERSION = 1


class IndexEntry(TypedDict, total=False):
    """One entry inside a per-machine index file."""

    job_id: str
    experiment_id: str
    condition: str
    status: str
    registered_at: str


class IndexFile(TypedDict):
    """Top-level shape of an exported index file."""

    schema_version: int
    machine_label: str
    exported_at: str
    entries: list[IndexEntry]


def _index_entry_for_job(job: Any) -> IndexEntry | None:
    """Project a signac job into an index entry, or None if non-terminal."""
    status = job.doc.get("status")
    if status not in TERMINAL_STATUSES:
        return None

    link = read_run_link(job.doc)
    entry: IndexEntry = {
        "job_id": job.id,
        "status": status,
    }
    exp_id = link.get("experiment_id") or job.sp.get("experiment_id")
    if exp_id:
        entry["experiment_id"] = str(exp_id)

    # `condition` is a common batch selector field but not universally
    # present. We capture it best-effort so batch citations like
    # `{type: batch, experiment_id: E005, selector: {condition: ...}}`
    # can be resolved against the index when Phase 1B's three-state
    # validator runs.
    condition = job.sp.get("condition")
    if condition:
        entry["condition"] = str(condition)

    registered_at = job.doc.get("created_at") or job.doc.get("started_at")
    if registered_at:
        entry["registered_at"] = str(registered_at)

    return entry


def build_index(repo_root: str | Path | None = None) -> IndexFile:
    """Walk the local run store and project into the IndexFile shape."""
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    machine_label = read_machine_label(root)
    entries: list[IndexEntry] = []
    try:
        project = get_run_store(root)
    except RunStoreNotInitialized:
        # No run store yet — emit an empty index. Calling this on a
        # laptop that's never registered anything is legitimate and
        # should produce a valid-but-empty file rather than crash.
        project = None  # type: ignore[assignment]

    if project is not None:
        for job in project:
            entry = _index_entry_for_job(job)
            if entry is not None:
                entries.append(entry)

    # Stable ordering: sort by job_id so repeat exports produce
    # byte-identical files when content is unchanged.
    entries.sort(key=lambda e: e.get("job_id", ""))

    return {
        "schema_version": SCHEMA_VERSION,
        "machine_label": machine_label,
        "exported_at": iso_utc_now(),
        "entries": entries,
    }


def export_index(
    repo_root: str | Path | None = None,
    *,
    out: Path | None = None,
    machine_label: str | None = None,
) -> Path:
    """Write the index file to disk and return its path.

    Parameters
    ----------
    repo_root : str | Path | None
        Consumer repo root. Defaults to ``find_repo_root()``.
    out : Path | None
        Output file path. Defaults to
        ``<repo_root>/.aexp/runs-index/<machine_label>.json``.
    machine_label : str | None
        Override for the machine label in both the filename and the
        body. Defaults to whatever ``read_machine_label(repo_root)``
        returns (typically the value from ``installed.json``).
    """
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    label = machine_label or read_machine_label(root)
    index = build_index(root)
    # Override the body's machine_label if the caller explicitly passed
    # one — so `--machine-label cluster` writes both the filename AND
    # the body field as `cluster`, even if installed.json says
    # something else.
    if machine_label is not None:
        index["machine_label"] = machine_label

    target = out if out is not None else (root / INDEX_DIR_REL / f"{label}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(index, indent=2) + "\n")
    return target


def load_all_indexes(repo_root: str | Path) -> dict[str, IndexFile]:
    """Read every per-machine index file under ``.aexp/runs-index/``.

    Returns a dict keyed by machine_label. Skips malformed files
    silently — a corrupt index file shouldn't break the validator
    (the worst case is that some "elsewhere" citations regress to
    "broken" until the file is regenerated).
    """
    root = Path(repo_root)
    index_dir = root / INDEX_DIR_REL
    out: dict[str, IndexFile] = {}
    if not index_dir.is_dir():
        return out
    for path in sorted(index_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "entries" not in data:
            continue
        label = data.get("machine_label") or path.stem
        out[str(label)] = data  # type: ignore[assignment]
    return out


def collect_known_elsewhere(repo_root: str | Path) -> dict[str, dict[str, Any]]:
    """Build the validator's "elsewhere" lookup from all index files.

    Returns a dict keyed by job_id, value = the IndexEntry dict plus
    a ``ledger_machine`` field naming the registering machine. The
    validator uses ``ledger_machine`` in the
    ``finding.absent_run_citation`` warning message so the user knows
    where the run actually lives.

    Phase 2's universal ledger supersedes this: when
    ``.aexp/ledger/<job_id>.json`` is the canonical source, the
    "elsewhere" category dissolves and this helper isn't called.
    """
    out: dict[str, dict[str, Any]] = {}
    for label, index in load_all_indexes(repo_root).items():
        for entry in index.get("entries", []):
            jid = entry.get("job_id")
            if not isinstance(jid, str):
                continue
            out[jid] = {**entry, "ledger_machine": label}
    return out


__all__ = [
    "INDEX_DIR_REL",
    "SCHEMA_VERSION",
    "IndexEntry",
    "IndexFile",
    "build_index",
    "collect_known_elsewhere",
    "export_index",
    "load_all_indexes",
]
