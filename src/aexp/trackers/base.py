"""TrackerAdapter ABC + ``bind_tracker`` helper.

An adapter translates a signac job + its linked Limina artifact into a
tracker-backend run, without owning any of the canonical state. After
``init_run``, the caller's ``bind_tracker`` writes the returned handle's
identity into ``job.doc["tracker"]`` so the link is persistent.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import signac

from aexp.limina_io import (
    ArtifactNotFoundError,
    find_artifact_path,
    load_experiment,
    load_hypothesis,
)
from aexp.schema import TrackerBinding, batch_slug

# ---------------------------------------------------------------------------
# Handle / Record
# ---------------------------------------------------------------------------


@dataclass
class RunHandle:
    """Opaque-to-callers reference to a live tracker run.

    Adapters subclass or reuse this directly; the important invariant is
    that ``id`` uniquely identifies the backend's run and ``url`` is a
    link the user can click.
    """

    id: str
    backend: str
    url: str | None = None
    project: str | None = None
    group: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunRecord:
    """Read-only view of a finished tracker run (for ``list_runs``)."""

    id: str
    group: str
    tags: tuple[str, ...]
    state: str
    summary: dict[str, Any]
    created_at: datetime | None


class TrackerInitError(RuntimeError):
    """Raised when an adapter can't initialize a run (missing SDK, auth, etc.)."""


# ---------------------------------------------------------------------------
# The ABC
# ---------------------------------------------------------------------------


class TrackerAdapter(ABC):
    """Abstract tracker interface.

    Adapters never own the manifest / kb artifact / run store. They only
    report to a remote backend.
    """

    #: Short human-readable backend name (``"noop"``, ``"wandb"``, ...).
    name: str = "abstract"

    @abstractmethod
    def init_run(
        self,
        *,
        project: str,
        group: str,
        tags: list[str],
        config: dict[str, Any],
        notes: str | None = None,
        offline: bool = False,
        workspace: str | None = None,
    ) -> RunHandle:
        """Start a tracker run; return a handle for subsequent log/finish calls.

        Parameters
        ----------
        workspace : str | None
            Absolute path to the signac job workspace for this run, if known.
            Adapters that produce local state (e.g. wandb's offline-run dir)
            should write it under this directory so everything for one run
            stays co-located. ``None`` means "use your backend's default".
        """

    @abstractmethod
    def log(self, handle: RunHandle, metrics: dict[str, Any]) -> None:
        """Log one step of metrics (step index is backend-specific)."""

    @abstractmethod
    def log_artifact(self, handle: RunHandle, name: str, path: Path) -> None:
        """Upload (or locally record) a file artifact by name."""

    @abstractmethod
    def finish(self, handle: RunHandle, *, exit_code: int = 0) -> None:
        """Mark the run finished."""

    @abstractmethod
    def list_runs(self, *, project: str, group_prefix: str) -> list[RunRecord]:
        """Return run records for a project/group prefix (observability side)."""


# ---------------------------------------------------------------------------
# bind_tracker — wires a signac job into a tracker run
# ---------------------------------------------------------------------------


def _curated_frame(job: signac.job.Job, repo_root: Path) -> dict[str, Any]:
    """Pull hypothesis statement + local hypothesis + success criteria from Limina.

    Never uploads raw markdown — just the curated text fields a tracker run
    page benefits from showing as context.
    """
    frame: dict[str, Any] = {}
    limina = job.doc.get("limina") or {}
    exp_id = limina.get("experiment_id")
    if not exp_id:
        return frame

    kb = repo_root / "kb"
    try:
        exp = load_experiment(exp_id, kb_root=kb)
    except Exception:
        exp = None

    if exp is not None:
        frame["experiment_title"] = exp.title
        # Extract "## Local Hypothesis" section if present.
        local = _extract_h2(exp.body, "Local Hypothesis")
        if local:
            frame["local_hypothesis"] = local
        success = _extract_h2(exp.body, "Expected Outcome") or _extract_h2(
            exp.body, "Success Criteria"
        )
        if success:
            frame["success_criteria"] = success

    hyp_id = limina.get("hypothesis_id")
    if hyp_id:
        try:
            hyp = load_hypothesis(hyp_id, kb_root=kb)
            statement = _extract_h2(hyp.body, "Statement")
            if statement:
                frame["hypothesis_statement"] = statement
        except Exception:
            pass

    return frame


def _extract_h2(body: str, heading: str) -> str | None:
    """Return the text under ``## heading`` in ``body`` (stripped), or None."""
    marker = f"## {heading}"
    if marker not in body:
        return None
    parts = body.split(marker, 1)[1].split("\n## ", 1)[0]
    return parts.strip() or None


def _flatten_sp(sp: Any) -> dict[str, Any]:
    """Best-effort flatten of a signac state-point SyncedAttrDict into a plain dict."""
    try:
        return dict(sp)
    except Exception:
        return {k: sp[k] for k in sp}  # type: ignore[index]


def build_tracker_config(
    job: signac.job.Job,
    repo_root: Path,
) -> dict[str, Any]:
    """Assemble the ``config`` payload passed to ``adapter.init_run``.

    Includes the full Limina chain (hypothesis, sub-hypothesis, experiment,
    experiment path) + the flattened state point + curated frame fields.
    """
    limina = dict(job.doc.get("limina") or {})
    config: dict[str, Any] = {
        **_flatten_sp(job.sp),
        "job_id": job.id,
        "limina": limina,
    }
    frame = _curated_frame(job, repo_root)
    if frame:
        config["frame"] = frame
    return config


def build_tracker_notes(job: signac.job.Job, repo_root: Path) -> str:
    """Assemble the ``notes`` string for the tracker run page.

    Prepends the hypothesis statement + local-hypothesis + experiment title
    so a reader can see *why* this run exists without clicking into config.
    """
    frame = _curated_frame(job, repo_root)
    parts: list[str] = []
    if "experiment_title" in frame:
        parts.append(f"# {frame['experiment_title']}")
    if "hypothesis_statement" in frame:
        parts.append(f"**Hypothesis:** {frame['hypothesis_statement']}")
    if "local_hypothesis" in frame:
        parts.append(f"**Local hypothesis:** {frame['local_hypothesis']}")
    if "success_criteria" in frame:
        parts.append(f"**Success criteria:** {frame['success_criteria']}")
    return "\n\n".join(parts)


def bind_tracker(
    job: signac.job.Job,
    adapter: TrackerAdapter,
    *,
    project: str,
    condition: str | None = None,
    extra_tags: list[str] | None = None,
    offline: bool = False,
    repo_root: str | Path | None = None,
) -> RunHandle:
    """Start a tracker run for ``job`` and stamp the binding onto ``job.doc``.

    Parameters
    ----------
    job : signac.job.Job
        The run to bind.
    adapter : TrackerAdapter
        Concrete adapter (e.g. ``WandbAdapter()``, ``NoopAdapter()``).
    project : str
        Backend project name (W&B project, etc.).
    condition : str | None
        Overrides ``job.sp["condition"]`` for the group slug, if you want
        a different grouping than the sp suggests.
    extra_tags : list[str] | None
        Additional tags to attach beyond the auto-derived ones.
    offline : bool
        Passed through to ``adapter.init_run``.
    repo_root : str | Path | None
        Consumer repo root for loading the Limina frame. Auto-detected if
        omitted.
    """
    from aexp.utils.paths import find_repo_root  # lazy to avoid circular

    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    limina = dict(job.doc.get("limina") or {})
    exp_id = limina.get("experiment_id") or job.sp.get("experiment_id")
    hyp_id = limina.get("hypothesis_id") or job.sp.get("hypothesis_id")
    cond = condition if condition is not None else job.sp.get("condition")

    group = batch_slug(
        hypothesis_id=hyp_id,
        experiment_id=exp_id or "E???",
        condition=cond,
        fallback=job.id[:8],
    )

    tags = ["kind=experiment"]
    if hyp_id:
        tags.append(hyp_id)
    if exp_id:
        tags.append(exp_id)
    sub = limina.get("sub_hypothesis_id")
    if sub:
        tags.append(sub)
    if cond:
        tags.append(f"condition={cond}")
    if extra_tags:
        tags.extend(extra_tags)

    config = build_tracker_config(job, root)
    notes = build_tracker_notes(job, root)

    handle = adapter.init_run(
        project=project,
        group=group,
        tags=tags,
        config=config,
        notes=notes or None,
        offline=offline,
        workspace=str(Path(job.path).resolve()),
    )

    binding = TrackerBinding(
        backend=adapter.name,
        run_id=handle.id,
        url=handle.url,
        project=project,
        group=group,
    )
    job.doc["tracker"] = binding.model_dump()
    return handle


__all__ = [
    "RunHandle",
    "RunRecord",
    "TrackerAdapter",
    "TrackerInitError",
    "bind_tracker",
    "build_tracker_config",
    "build_tracker_notes",
]
