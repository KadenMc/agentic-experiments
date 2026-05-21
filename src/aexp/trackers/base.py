"""TrackerAdapter ABC + ``bind_tracker`` helper.

An adapter translates a signac job + its linked research artifacts into a
tracker-backend run, without owning any of the canonical state. After
``init_run``, the caller's ``bind_tracker`` writes the returned handle's
identity into ``job.doc["tracker"]`` so the link is persistent.
"""
from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import signac

from aexp.kb_io import (
    load_experiment,
    load_hypothesis,
)
from aexp.schema import TrackerBinding, batch_slug, read_run_link

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
# Tracker context — the preferred wandb surface
# ---------------------------------------------------------------------------


# Keys ``prepare_tracker`` owns inside ``init_kwargs``. Callers that splat
# ``**ctx.init_kwargs`` into ``wandb.init(**...)`` and pass additional kwargs
# alongside (e.g. ``name=``, ``job_type=``) must not collide with these — or
# if they do, Python's later-wins dict-splat rule applies and the caller's
# value silently wins. ``tracked_run`` enforces aexp-owned precedence
# deliberately.
_AEXP_OWNED_INIT_KEYS: frozenset[str] = frozenset(
    {"project", "group", "tags", "config", "notes", "dir", "mode"}
)


def _derive_tracker_payload(
    job: signac.job.Job,
    *,
    project: str,
    condition: str | None,
    extra_tags: list[str] | None,
    offline: bool,
    repo_root: Path,
    entity: str | None,
) -> tuple[str, list[str], dict[str, Any]]:
    """Core derivation: returns ``(group, tags, init_kwargs)``.

    Shared between :func:`prepare_tracker` and :func:`bind_tracker` so the
    legacy adapter path and the new BYO-init path compute identical
    metadata. Pure function; does not touch ``job.doc``.
    """
    link = read_run_link(job.doc)
    exp_id = link.get("experiment_id") or job.sp.get("experiment_id")
    hyp_id = link.get("hypothesis_id") or job.sp.get("hypothesis_id")
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
    sub = link.get("sub_hypothesis_id")
    if sub:
        tags.append(sub)
    if cond:
        tags.append(f"condition={cond}")
    if extra_tags:
        tags.extend(extra_tags)

    config = build_tracker_config(job, repo_root)
    notes = build_tracker_notes(job, repo_root) or None

    init_kwargs: dict[str, Any] = {
        "project": project,
        "group": group,
        "tags": list(tags),
        "config": config,
        "notes": notes,
        "dir": str(Path(job.path).resolve()),
        "reinit": True,
    }
    if offline:
        init_kwargs["mode"] = "offline"
    if entity is not None:
        init_kwargs["entity"] = entity

    return group, tags, init_kwargs


@dataclass(frozen=True)
class TrackerContext:
    """Payload for a tracker binding — computed, not yet attached to a run.

    Returned by :func:`prepare_tracker`. Holds everything a caller needs to
    either (a) splat ``init_kwargs`` into ``wandb.init`` themselves and then
    call :meth:`bind`, or (b) hand the context to :func:`tracked_run` /
    :func:`bind_tracker`, which do both steps.

    All fields are wandb-shaped. :meth:`bind` is backend-agnostic — it
    duck-types ``run.id`` and ``run.url`` and writes a ``TrackerBinding``
    into ``job.doc["tracker"]``. The ``backend`` argument defaults to
    ``"wandb"`` since that's the 99% case; pass a different name if you
    adopted the context for a non-wandb run.

    ``init_kwargs`` intentionally omits ``name``, ``job_type``, ``resume``,
    and ``settings`` — those are caller-owned. Callers who splat
    ``**ctx.init_kwargs`` plus their own kwargs should not overwrite the
    aexp-owned keys listed in ``_AEXP_OWNED_INIT_KEYS`` unless they know
    what they're doing.
    """

    job: signac.job.Job
    project: str
    group: str
    tags: list[str]
    init_kwargs: dict[str, Any]

    def bind(self, run: Any, *, backend: str = "wandb") -> TrackerBinding:
        """Stamp ``job.doc["tracker"]`` from an initialized run.

        Reads ``run.id`` (required) and ``run.url`` (optional). Writes a
        ``TrackerBinding`` dict to ``self.job.doc["tracker"]`` and returns
        the binding.
        """
        binding = TrackerBinding(
            backend=backend,
            run_id=str(getattr(run, "id", "") or ""),
            url=getattr(run, "url", None),
            project=self.project,
            group=self.group,
        )
        self.job.doc["tracker"] = binding.model_dump()
        return binding


def prepare_tracker(
    job: signac.job.Job,
    *,
    project: str,
    condition: str | None = None,
    extra_tags: list[str] | None = None,
    offline: bool = False,
    repo_root: str | Path | None = None,
    entity: str | None = None,
) -> TrackerContext:
    """Compute the tracker payload for a signac job without starting a run.

    Derives the deterministic group slug, auto-tags, curated research frame,
    notes, and the flattened state point, then packages them as wandb-shaped
    ``init_kwargs``. The caller owns ``wandb.init`` and run lifecycle; invoke
    :meth:`TrackerContext.bind` after ``wandb.init`` to write the signac
    binding.

    Parameters
    ----------
    job
        The signac job to derive payload for.
    project
        Backend project name (W&B project, etc.).
    condition
        Overrides ``job.sp["condition"]`` for the group slug, if different.
    extra_tags
        Additional tags to attach beyond the auto-derived ones.
    offline
        If ``True``, ``init_kwargs`` will include ``mode="offline"``.
    repo_root
        Consumer repo root for loading the research frame. Auto-detected
        if omitted.
    entity
        Optional W&B entity (team / user). Threaded through ``init_kwargs``
        so callers owning ``wandb.init`` don't need to re-specify.
    """
    from aexp.utils.paths import find_repo_root  # lazy to avoid circular

    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    group, tags, init_kwargs = _derive_tracker_payload(
        job,
        project=project,
        condition=condition,
        extra_tags=extra_tags,
        offline=offline,
        repo_root=root,
        entity=entity,
    )
    return TrackerContext(
        job=job,
        project=project,
        group=group,
        tags=tags,
        init_kwargs=init_kwargs,
    )


@contextlib.contextmanager
def tracked_run(
    job: signac.job.Job,
    *,
    project: str,
    condition: str | None = None,
    extra_tags: list[str] | None = None,
    offline: bool = False,
    repo_root: str | Path | None = None,
    entity: str | None = None,
    name: str | None = None,
    job_type: str | None = None,
    **wandb_kwargs: Any,
) -> Iterator[Any]:
    """Managed wandb run: prepare + ``wandb.init`` + bind + ``run.finish``.

    Yields the live ``wandb.Run``. The full wandb surface is available on
    the yielded object — ``run.log()``, ``run.log_artifact()``,
    ``run.summary[...]``, ``run.define_metric()``, ``wandb.Table`` uploads,
    ``run.alert()``, everything. aexp's only touch is the init payload and
    the signac binding; all logging is yours.

    ``name`` and ``job_type`` are caller-owned and passed through to
    ``wandb.init``. ``**wandb_kwargs`` is a last-resort merge surface for
    things aexp doesn't model (``resume``, ``settings``, ``save_code``).

    Precedence rule: aexp-owned keys in ``TrackerContext.init_kwargs``
    (``project``/``group``/``tags``/``config``/``notes``/``dir``/``mode``)
    are authoritative here — conflicting values in ``**wandb_kwargs`` are
    dropped with no warning. Callers who need full control should use
    :func:`prepare_tracker` and call ``wandb.init`` themselves.

    Does not manage signac status transitions — compose
    :func:`aexp.runs.run_lifecycle` alongside this if you want both::

        with run_lifecycle(job), tracked_run(job, project="foo") as run:
            run.log({...})

    Raises :class:`TrackerInitError` if ``wandb`` isn't installed.
    """
    try:
        import wandb  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TrackerInitError(
            "wandb is not installed; install agentic-experiments[wandb] "
            "to use tracked_run"
        ) from exc

    ctx = prepare_tracker(
        job,
        project=project,
        condition=condition,
        extra_tags=extra_tags,
        offline=offline,
        repo_root=repo_root,
        entity=entity,
    )

    # Merge caller-supplied kwargs, then override with aexp-owned keys so the
    # discipline wins. name / job_type are NOT aexp-owned, so they land as
    # caller values.
    merged: dict[str, Any] = {**wandb_kwargs}
    if name is not None:
        merged["name"] = name
    if job_type is not None:
        merged["job_type"] = job_type
    for key in _AEXP_OWNED_INIT_KEYS:
        if key in ctx.init_kwargs:
            merged[key] = ctx.init_kwargs[key]
    merged.setdefault("reinit", ctx.init_kwargs.get("reinit", True))
    if "entity" in ctx.init_kwargs:
        merged.setdefault("entity", ctx.init_kwargs["entity"])

    try:
        run = wandb.init(**merged)
    except Exception as exc:
        raise TrackerInitError(f"wandb.init failed: {exc}") from exc

    ctx.bind(run, backend="wandb")

    exit_code = 0
    try:
        yield run
    except BaseException:
        exit_code = 1
        raise
    finally:
        try:
            run.finish(exit_code=exit_code)
        except Exception:
            # Never let a finish failure mask the original exception.
            pass


# ---------------------------------------------------------------------------
# bind_tracker — wires a signac job into a tracker run via an adapter
# ---------------------------------------------------------------------------


def _curated_frame(job: signac.job.Job, repo_root: Path) -> dict[str, Any]:
    """Pull hypothesis statement + local hypothesis + success criteria from the kb/ artifacts.

    Never uploads raw markdown — just the curated text fields a tracker run
    page benefits from showing as context.
    """
    frame: dict[str, Any] = {}
    link = read_run_link(job.doc)
    exp_id = link.get("experiment_id")
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

    hyp_id = link.get("hypothesis_id")
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

    Includes the full run-link chain (hypothesis, sub-hypothesis, experiment,
    experiment path) + the flattened state point + curated frame fields.
    """
    link = read_run_link(job.doc)
    config: dict[str, Any] = {
        **_flatten_sp(job.sp),
        "job_id": job.id,
        "aexp": link,
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

    For wandb, prefer :func:`tracked_run` (aexp owns init + finish) or
    :func:`prepare_tracker` (caller owns init). This adapter-mediated path
    stays for backend-agnostic code (e.g. :class:`NoopAdapter`) and for
    backward compatibility with existing integrations.

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
        Consumer repo root for loading the research frame. Auto-detected if
        omitted.
    """
    from aexp.utils.paths import find_repo_root  # lazy to avoid circular

    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    group, tags, init_kwargs = _derive_tracker_payload(
        job,
        project=project,
        condition=condition,
        extra_tags=extra_tags,
        offline=offline,
        repo_root=root,
        entity=None,
    )

    handle = adapter.init_run(
        project=project,
        group=group,
        tags=tags,
        config=init_kwargs["config"],
        notes=init_kwargs.get("notes"),
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
    "TrackerContext",
    "TrackerInitError",
    "bind_tracker",
    "build_tracker_config",
    "build_tracker_notes",
    "prepare_tracker",
    "tracked_run",
]
