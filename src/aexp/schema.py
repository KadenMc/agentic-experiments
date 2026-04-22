"""Pydantic + dataclass models for the fusion layer.

The types here are the *lingua franca* between signac jobs, Limina artifacts,
tracker adapters, and the CLI. They deliberately stay small and frozen-ish;
business logic lives in the modules that produce / consume them.

Conventions
-----------
- ``RunLink``: the canonical shape of ``job.doc["limina"]``.
- ``SupportingRun`` / ``BatchSelector``: entries in a Finding's
  ``supporting_runs:`` frontmatter list (see plan §2, §8).
- ``LiminaArtifactRef``: typed handle returned by ``limina_io`` readers.
- ``RunSummary``: flat summary row for CLI ``list-runs`` / ``list-batches``.
- ``Issue``: an actionable validator finding; the CLI prints these.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared literal type sets
# ---------------------------------------------------------------------------

ArtifactKind = Literal["H", "E", "F", "L", "CR", "SR"]
"""The six Limina artifact kinds validated by vendored ``kb_validate.py``.

``H``=Hypothesis, ``E``=Experiment, ``F``=Finding, ``L``=Literature,
``CR``=Challenge Review, ``SR``=Strategic Review.
"""

RunStatus = Literal["created", "running", "complete", "failed", "abandoned"]
"""Lifecycle values written to ``job.doc["status"]`` (plan §6)."""

IssueSeverity = Literal["error", "warning"]
"""Validator issue severity. Errors fail ``aex validate``; warnings do not."""


# ---------------------------------------------------------------------------
# Limina <-> signac link
# ---------------------------------------------------------------------------


class RunLink(BaseModel):
    """Canonical shape of ``job.doc["limina"]``.

    Mirrors the invariants in plan §2: every tracked run must reference at
    least an experiment; hypothesis is optional when a run links to an
    experiment (via the experiment's primary hypothesis); a sub-hypothesis
    may further specialize the framing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = Field(..., pattern=r"^E\d{3}$")
    experiment_path: str = Field(
        ...,
        description="Repo-relative POSIX path to kb/research/experiments/E###-*.md",
    )
    hypothesis_id: str | None = Field(default=None, pattern=r"^H\d{3}$")
    sub_hypothesis_id: str | None = Field(default=None, pattern=r"^H\d{3}$")


class TrackerBinding(BaseModel):
    """Shape of ``job.doc["tracker"]`` after ``bind_tracker`` (plan §7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str
    run_id: str
    url: str | None = None
    project: str | None = None
    group: str | None = None


# ---------------------------------------------------------------------------
# Finding supporting_runs schema
# ---------------------------------------------------------------------------


class SupportingJobRun(BaseModel):
    """A finding's citation of a single signac job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["job"] = "job"
    id: str


class BatchSelector(BaseModel):
    """A state-point slice identifying a batch of signac jobs.

    Used both as a query (``aex show-batch``) and as a finding citation
    (``supporting_runs: [{type: batch, experiment_id: E018, selector: ...}]``).
    The selector dict is matched against each job's ``sp`` by exact equality.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["batch"] = "batch"
    experiment_id: str = Field(..., pattern=r"^E\d{3}$")
    selector: dict[str, Any] = Field(default_factory=dict)


SupportingRun = SupportingJobRun | BatchSelector
"""Union of the two ways a Finding can cite runs."""


# ---------------------------------------------------------------------------
# Limina artifact reference (read-only handle returned by limina_io)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiminaArtifactRef:
    """A typed pointer to one Limina artifact on disk.

    Returned by ``limina_io.load_*`` helpers. The raw frontmatter dict is
    exposed as ``metadata`` so callers can read fields we don't model.
    """

    kind: ArtifactKind
    id: str
    path: str  # repo-relative POSIX
    title: str
    metadata: dict[str, Any]
    body: str


# ---------------------------------------------------------------------------
# Run summary (one row per job in CLI listings)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    """Flat summary of one signac job for table-style CLI output."""

    job_id: str
    experiment_id: str | None
    hypothesis_id: str | None
    status: RunStatus | None
    batch_slug: str | None
    tracker_url: str | None
    sp: dict[str, Any]
    started_at: str | None
    ended_at: str | None


@dataclass(frozen=True)
class BatchSummary:
    """Aggregate row for ``aex list-batches`` / ``aex show-batch``."""

    experiment_id: str
    batch_slug: str
    selector: dict[str, Any]
    count: int
    status_counts: dict[RunStatus, int]
    tracker_group: str | None


# ---------------------------------------------------------------------------
# Validator issue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """An actionable validator finding; printed by ``aex validate``.

    The codes align with plan §8. ``path`` is the repo-relative POSIX path
    of the offending file / directory. ``detail`` may include a field
    locator (e.g. ``"hypothesis_id"``) or free-form prose.
    """

    code: str
    message: str
    severity: IssueSeverity = "error"
    path: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Helpers (stateless; used by multiple modules)
# ---------------------------------------------------------------------------


def batch_slug(
    *,
    hypothesis_id: str | None,
    experiment_id: str,
    condition: str | None,
    fallback: str,
) -> str:
    """Deterministic slug used for W&B group + batch identity (plan §2, §7).

    Shape: ``"{hypothesis_id or '_'}/{experiment_id}/{condition or fallback}"``.
    The fallback is typically the job's short id — keeps the slug informative
    when a run has no ``condition`` in its state point.
    """
    hyp = hypothesis_id if hypothesis_id else "_"
    cond = condition if condition else fallback
    return f"{hyp}/{experiment_id}/{cond}"


def iso_utc_now() -> str:
    """Current UTC timestamp in ISO-8601 with seconds precision.

    Separate helper so tests can monkeypatch a deterministic clock.
    """
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


__all__ = [
    "ArtifactKind",
    "BatchSelector",
    "BatchSummary",
    "Issue",
    "IssueSeverity",
    "LiminaArtifactRef",
    "RunLink",
    "RunStatus",
    "RunSummary",
    "SupportingJobRun",
    "SupportingRun",
    "TrackerBinding",
    "batch_slug",
    "date",
    "iso_utc_now",
]
