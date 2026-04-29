"""Repo-level validator — composes KB structural checks with run-link checks.

Plan §8 lays out the full set of checks:

1. Call :func:`aexp.kb_validate.validate_kb` in-process and surface its
   errors as ``Issue`` rows (code ``limina.validation_failed``).
2. Walk ``.runs/workspace/*`` and for each job:
   - ``run.orphan`` if ``doc["limina"]`` is missing.
   - ``run.broken_experiment_link`` if the referenced E### file doesn't exist.
   - ``run.hypothesis_mismatch`` if the run's hypothesis_id contradicts the
     experiment's Hypothesis frontmatter.
   - ``run.sub_hypothesis_unlisted`` if sub_hypothesis_id isn't a registered
     sub-hypothesis of the experiment.
   - ``run.status_invalid`` if ``doc["status"]`` isn't in the allowed set.
3. Walk findings and verify ``supporting_runs:`` citations exist:
   - ``finding.broken_run_citation`` — a job id that doesn't resolve.
   - ``finding.empty_batch`` — a batch selector that matches zero jobs.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from aexp.kb_validate import format_text as _kb_format_text
from aexp.kb_validate import validate_kb
from aexp.limina_io import (
    ArtifactNotFoundError,
    find_artifact_path,
    list_kb_artifacts,
)
from aexp.linking import list_batches
from aexp.runs import get_run_store
from aexp.schema import Issue, RunStatus
from aexp.utils.paths import find_repo_root

VALID_STATUSES: set[RunStatus] = {
    "created",
    "running",
    "complete",
    "failed",
    "abandoned",
    "stopped",
}
# Note: "queued" is intentionally *not* validated as a "valid" terminal
# status here — queued jobs are pending, not steady-state. Validator
# scope is post-hoc analysis of finished runs.

ValidateMode = Literal["full", "kb-only", "runs-only"]


class ValidateResult:
    """A validation run's outcome — Issues + an easy boolean."""

    __slots__ = ("issues",)

    def __init__(self, issues: list[Issue] | None = None) -> None:
        self.issues: list[Issue] = list(issues or [])

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.issues)

    def __iter__(self):  # pragma: no cover - trivial
        return iter(self.issues)


# ---------------------------------------------------------------------------
# In-process KB structural validation
# ---------------------------------------------------------------------------


def _run_kb_validate(repo_root: Path) -> list[Issue]:
    """Run KB structural validation in-process via :func:`validate_kb`.

    Returns an empty list if there is no ``kb/`` directory (not-installed
    case — callers decide whether that's worth flagging separately).

    No subprocess, no env manipulation, no telemetry concerns — all three
    were required back when this shelled out to the vendored
    ``scripts/kb_validate.py``. That script is now importable as
    ``aexp.kb_validate``.
    """
    kb_root = repo_root / "kb"
    if not kb_root.is_dir():
        return []

    try:
        result = validate_kb(kb_root)
    except Exception as exc:
        return [
            Issue(
                code="limina.validator_unavailable",
                message=f"kb_validate could not run: {exc}",
                severity="error",
            )
        ]

    if result.ok:
        return []

    message = _kb_format_text(result)
    return [
        Issue(
            code="limina.validation_failed",
            message=message,
            severity="error",
        )
    ]


# ---------------------------------------------------------------------------
# Run-link checks
# ---------------------------------------------------------------------------


def _check_run_links(repo_root: Path) -> list[Issue]:
    """Validate ``job.doc["limina"]`` + ``doc["status"]`` for every run."""
    issues: list[Issue] = []
    try:
        project = get_run_store(repo_root)
    except Exception:
        return []

    kb_root = repo_root / "kb"

    for job in project:
        rel = f".runs/workspace/{job.id}"
        status = job.doc.get("status")
        if status is not None and status not in VALID_STATUSES:
            issues.append(
                Issue(
                    code="run.status_invalid",
                    message=(
                        f"run {job.id[:8]} has status={status!r}; expected one of "
                        f"{sorted(VALID_STATUSES)}"
                    ),
                    path=rel,
                )
            )

        link = job.doc.get("limina")
        if not link:
            issues.append(
                Issue(
                    code="run.orphan",
                    message=(
                        f"run {job.id[:8]} has no doc['limina'] — "
                        "link it with `aex link <id> --experiment E###`"
                    ),
                    path=rel,
                )
            )
            continue

        exp_id = link.get("experiment_id")
        if not exp_id:
            issues.append(
                Issue(
                    code="run.broken_experiment_link",
                    message=f"run {job.id[:8]} has doc['limina'] without experiment_id",
                    path=rel,
                )
            )
            continue

        # Resolve experiment artifact on disk.
        try:
            exp_path = find_artifact_path(exp_id, kb_root=kb_root)
        except (ArtifactNotFoundError, Exception):
            issues.append(
                Issue(
                    code="run.broken_experiment_link",
                    message=(
                        f"run {job.id[:8]} references experiment {exp_id} "
                        "but no matching kb/research/experiments/ file was found"
                    ),
                    path=rel,
                    detail=f"experiment_id={exp_id}",
                )
            )
            continue

        # Parse the experiment's frontmatter to check hypothesis agreement.
        import frontmatter  # local import to keep module import fast

        try:
            post = frontmatter.load(exp_path)
            fm = post.metadata
        except Exception:
            continue

        run_hyp = link.get("hypothesis_id")
        exp_hyp = str(fm.get("hypothesis", "")).strip() or None
        exp_sub_hyps = fm.get("sub_hypotheses") or []
        if isinstance(exp_sub_hyps, str):
            # Tolerate a scalar by normalizing to a list.
            exp_sub_hyps = [exp_sub_hyps]
        exp_sub_hyps = [str(h).strip() for h in exp_sub_hyps]

        if run_hyp and exp_hyp and run_hyp != exp_hyp and run_hyp not in exp_sub_hyps:
            issues.append(
                Issue(
                    code="run.hypothesis_mismatch",
                    message=(
                        f"run {job.id[:8]} hypothesis_id={run_hyp} but "
                        f"experiment {exp_id} has Hypothesis={exp_hyp} and "
                        f"Sub-hypotheses={exp_sub_hyps}"
                    ),
                    path=rel,
                    detail=f"experiment_id={exp_id}",
                )
            )

        sub = link.get("sub_hypothesis_id")
        if sub and sub not in exp_sub_hyps:
            issues.append(
                Issue(
                    code="run.sub_hypothesis_unlisted",
                    message=(
                        f"run {job.id[:8]} sub_hypothesis_id={sub} is not listed in "
                        f"experiment {exp_id} Sub-hypotheses: {exp_sub_hyps}"
                    ),
                    path=rel,
                    detail=f"experiment_id={exp_id}",
                )
            )

    return issues


# ---------------------------------------------------------------------------
# Finding supporting_runs checks
# ---------------------------------------------------------------------------


_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _check_finding_citations(repo_root: Path) -> list[Issue]:
    """Validate ``supporting_runs:`` entries in every finding."""
    issues: list[Issue] = []
    kb_root = repo_root / "kb"
    if not kb_root.is_dir():
        return []

    try:
        project = get_run_store(repo_root)
    except Exception:
        project = None  # type: ignore[assignment]

    try:
        findings = list_kb_artifacts(kb_root, kind="F")
    except Exception:
        findings = []

    known_job_ids: set[str] = set(j.id for j in project) if project is not None else set()

    for finding in findings:
        citations = finding.metadata.get("supporting_runs") or []
        if isinstance(citations, dict):
            citations = [citations]
        if not isinstance(citations, list):
            continue

        for idx, raw in enumerate(citations):
            if not isinstance(raw, dict):
                got_type = type(raw).__name__
                hint = (
                    f"got {got_type} ({raw!r:.60}); expected a mapping like "
                    "{type: job, id: <32-hex>} or "
                    "{type: batch, experiment_id: E###, selector: {condition: ...}}"
                )
                issues.append(
                    Issue(
                        code="finding.broken_run_citation",
                        message=(
                            f"finding {finding.id}: supporting_runs[{idx}] is not a mapping — "
                            + hint
                        ),
                        path=finding.path,
                    )
                )
                continue
            ctype = raw.get("type")
            if ctype == "job":
                jid = raw.get("id")
                if not isinstance(jid, str) or not _JOB_ID_RE.match(jid):
                    issues.append(
                        Issue(
                            code="finding.broken_run_citation",
                            message=(
                                f"finding {finding.id}: supporting_runs[{idx}].id "
                                f"is not a 32-hex job id (got {jid!r})"
                            ),
                            path=finding.path,
                        )
                    )
                    continue
                if project is not None and jid not in known_job_ids:
                    issues.append(
                        Issue(
                            code="finding.broken_run_citation",
                            message=(
                                f"finding {finding.id}: supporting_runs[{idx}] "
                                f"references job {jid} which does not exist in .runs/"
                            ),
                            path=finding.path,
                            detail=f"job_id={jid}",
                        )
                    )
            elif ctype == "batch":
                exp_id = raw.get("experiment_id")
                selector = raw.get("selector") or {}
                if not isinstance(exp_id, str):
                    issues.append(
                        Issue(
                            code="finding.broken_run_citation",
                            message=(
                                f"finding {finding.id}: supporting_runs[{idx}] "
                                "batch citation missing 'experiment_id'"
                            ),
                            path=finding.path,
                        )
                    )
                    continue
                if project is None:
                    continue  # can't resolve without a run store
                batches = list_batches(experiment_id=exp_id, repo_root=repo_root)
                matches = [b for b in batches if _selector_matches(b.selector, selector)]
                if not matches or all(b.count == 0 for b in matches):
                    issues.append(
                        Issue(
                            code="finding.empty_batch",
                            message=(
                                f"finding {finding.id}: batch citation for "
                                f"experiment={exp_id} selector={selector} "
                                "matches no runs"
                            ),
                            path=finding.path,
                        )
                    )
            else:
                hint = (
                    "expected type='job' with an 'id' (32-hex job id) OR "
                    "type='batch' with 'experiment_id' and 'selector'"
                )
                issues.append(
                    Issue(
                        code="finding.broken_run_citation",
                        message=(
                            f"finding {finding.id}: supporting_runs[{idx}] has "
                            f"unknown or missing type={ctype!r} — {hint}"
                        ),
                        path=finding.path,
                    )
                )
    return issues


def _selector_matches(batch_selector: dict[str, Any], query: dict[str, Any]) -> bool:
    """True when every query key is present in batch_selector with the same value."""
    if not query:
        return True
    return all(batch_selector.get(k) == v for k, v in query.items())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_repo(
    repo_root: str | Path | None = None,
    *,
    mode: ValidateMode = "full",
) -> ValidateResult:
    """Run the full repo validation.

    Parameters
    ----------
    repo_root : str | Path | None
        Consumer repo root. Defaults to ``find_repo_root()``.
    mode : {"full", "kb-only", "runs-only"}
        - ``full`` (default): all checks.
        - ``kb-only``: only invoke ``kb_validate.py``; skip signac-side checks.
        - ``runs-only``: only run the run-link + finding-citation checks.
    """
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    result = ValidateResult()

    if mode in ("full", "kb-only"):
        for issue in _run_kb_validate(root):
            result.add(issue)

    if mode in ("full", "runs-only"):
        for issue in _check_run_links(root):
            result.add(issue)
        for issue in _check_finding_citations(root):
            result.add(issue)

    return result


__all__ = [
    "VALID_STATUSES",
    "ValidateMode",
    "ValidateResult",
    "validate_repo",
]
