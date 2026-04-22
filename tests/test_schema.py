"""Tests for pydantic + dataclass models in ``schema.py``."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from aexp.schema import (
    BatchSelector,
    Issue,
    LiminaArtifactRef,
    RunLink,
    RunSummary,
    SupportingJobRun,
    TrackerBinding,
    batch_slug,
    iso_utc_now,
)

# ---------------------------------------------------------------------------
# RunLink
# ---------------------------------------------------------------------------


def test_runlink_requires_valid_experiment_id() -> None:
    with pytest.raises(ValidationError):
        RunLink(experiment_id="E1", experiment_path="kb/.../E001-foo.md")


def test_runlink_accepts_optional_hypothesis() -> None:
    link = RunLink(
        experiment_id="E018",
        experiment_path="kb/research/experiments/E018-foo.md",
        hypothesis_id="H012",
    )
    assert link.hypothesis_id == "H012"
    assert link.sub_hypothesis_id is None


def test_runlink_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RunLink(
            experiment_id="E001",
            experiment_path="kb/.../E001-foo.md",
            rogue="nope",  # type: ignore[call-arg]
        )


def test_runlink_is_frozen() -> None:
    link = RunLink(experiment_id="E001", experiment_path="kb/x.md")
    with pytest.raises(ValidationError):
        link.experiment_id = "E002"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SupportingRun union
# ---------------------------------------------------------------------------


def test_supporting_job_run_defaults_type_literal() -> None:
    entry = SupportingJobRun(id="abcd1234")
    assert entry.type == "job"
    assert entry.id == "abcd1234"


def test_batch_selector_keeps_selector_dict() -> None:
    entry = BatchSelector(experiment_id="E018", selector={"condition": "full"})
    assert entry.type == "batch"
    assert entry.selector["condition"] == "full"


def test_batch_selector_requires_valid_experiment_id() -> None:
    with pytest.raises(ValidationError):
        BatchSelector(experiment_id="bad")


# ---------------------------------------------------------------------------
# TrackerBinding
# ---------------------------------------------------------------------------


def test_tracker_binding_minimal() -> None:
    b = TrackerBinding(backend="noop", run_id="r_1")
    assert b.backend == "noop"
    assert b.url is None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def test_limina_artifact_ref_is_frozen() -> None:
    ref = LiminaArtifactRef(
        kind="E",
        id="E001",
        path="kb/research/experiments/E001-foo.md",
        title="Foo",
        metadata={"hypothesis": "H001"},
        body="",
    )
    with pytest.raises(AttributeError):
        ref.id = "E002"  # type: ignore[misc]


def test_run_summary_carries_state_point_dict() -> None:
    s = RunSummary(
        job_id="jid",
        experiment_id="E001",
        hypothesis_id="H001",
        status="complete",
        batch_slug="H001/E001/full",
        tracker_url=None,
        sp={"condition": "full", "seed": 0},
        started_at=None,
        ended_at=None,
    )
    assert s.sp == {"condition": "full", "seed": 0}


def test_issue_defaults_to_error_severity() -> None:
    i = Issue(code="x.y", message="nope")
    assert i.severity == "error"
    assert i.path is None


# ---------------------------------------------------------------------------
# batch_slug helper
# ---------------------------------------------------------------------------


def test_batch_slug_with_all_fields() -> None:
    assert (
        batch_slug(hypothesis_id="H012", experiment_id="E018", condition="full", fallback="x")
        == "H012/E018/full"
    )


def test_batch_slug_underscores_missing_hypothesis() -> None:
    assert (
        batch_slug(hypothesis_id=None, experiment_id="E001", condition="full", fallback="jid")
        == "_/E001/full"
    )


def test_batch_slug_falls_back_when_condition_missing() -> None:
    assert (
        batch_slug(
            hypothesis_id="H001",
            experiment_id="E001",
            condition=None,
            fallback="abcd1234",
        )
        == "H001/E001/abcd1234"
    )


# ---------------------------------------------------------------------------
# iso_utc_now
# ---------------------------------------------------------------------------


def test_iso_utc_now_shape() -> None:
    s = iso_utc_now()
    # Shape: "YYYY-MM-DDTHH:MM:SSZ", 20 chars
    assert len(s) == 20
    assert s.endswith("Z")
    assert s[10] == "T"
