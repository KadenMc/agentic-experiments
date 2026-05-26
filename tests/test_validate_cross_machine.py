"""Tests for the cross-machine validation features.

Covers ``--strict-runs={error|warn|off}`` (Phase 1A) and — once Phases 1B and 2
land — the per-machine index three-state vocabulary and the ledger-source
switch.

The test fixtures here mirror the broken-citation + empty-batch shapes from
``test_validate.py`` so we exercise the same code paths from the citation
check, just with the new severity policy in play.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from aexp.install import install_scaffold
from aexp.validate import validate_repo


def _git_commit(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture_output=True
    )
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _write_artifact(
    kb_root: Path,
    subdir: str,
    filename: str,
    frontmatter: dict,
    body: str,
    *,
    links: list[str] | None = None,
) -> Path:
    """Write a minimally-conforming kb/ artifact (aliases + Links + headers)."""
    fm = dict(frontmatter)
    aid = fm.get("id")
    if aid and "aliases" not in fm:
        fm["aliases"] = [aid]
    target = kb_root / subdir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(fm, sort_keys=False).strip()

    trailing = body

    artifact_type = fm.get("type", "")
    kind = {"hypothesis": "H", "experiment": "E", "finding": "F"}.get(artifact_type)
    if kind:
        from aexp.kb_validate import _required_headers_for_kind

        for header in _required_headers_for_kind(kind):
            marker = f"## {header}"
            if marker not in trailing:
                trailing = trailing.rstrip() + f"\n\n{marker}\n\n_(placeholder for tests.)_\n"

    if "## Links" not in trailing:
        link_lines = "\n".join(f"- [[{link}]]" for link in (links or [])) or "- (none)"
        trailing = trailing.rstrip() + f"\n\n## Links\n\n{link_lines}\n"

    target.write_text(f"---\n{fm_text}\n---\n\n{trailing}", encoding="utf-8")
    return target


@pytest.fixture
def installed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_commit(repo)
    install_scaffold(repo)
    return repo


def _seed_h_e_artifacts(kb: Path) -> None:
    """Materialize a hypothesis + experiment so kb_validate is happy."""
    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-a.md",
        {"id": "H001", "type": "hypothesis", "status": "PROPOSED", "created": "2026-04-20"},
        "# H001 — A\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n",
    )
    _write_artifact(
        kb,
        "research/experiments",
        "E001-a.md",
        {
            "id": "E001",
            "type": "experiment",
            "status": "DESIGNED",
            "hypothesis": "H001",
            "created": "2026-04-20",
        },
        "# E001 — A\n> **Status**: DESIGNED\n> **Hypothesis**: [[H001]]\n> **Created**: 2026-04-20\n",
    )


def _write_finding_citing_missing_job(kb: Path) -> None:
    """Drop F001 citing a 32-hex job that doesn't exist locally."""
    _write_artifact(
        kb,
        "research/findings",
        "F001-a.md",
        {
            "id": "F001",
            "type": "finding",
            "hypothesis": "H001",
            "experiment": "E001",
            "impact": "moderate",
            "created": "2026-04-20",
            "supporting_runs": [{"type": "job", "id": "f" * 32}],
        },
        "# F001 — A\n> **Hypothesis**: [[H001]]\n> **Experiment**: [[E001]]\n> **Impact**: moderate\n> **Created**: 2026-04-20\n",
    )


def _write_finding_citing_empty_batch(kb: Path) -> None:
    """Drop F001 citing a batch selector that matches no local jobs."""
    _write_artifact(
        kb,
        "research/findings",
        "F001-a.md",
        {
            "id": "F001",
            "type": "finding",
            "hypothesis": "H001",
            "experiment": "E001",
            "impact": "moderate",
            "created": "2026-04-20",
            "supporting_runs": [
                {"type": "batch", "experiment_id": "E001", "selector": {"condition": "ghost"}}
            ],
        },
        "# F001\n> **Hypothesis**: [[H001]]\n> **Experiment**: [[E001]]\n> **Impact**: moderate\n> **Created**: 2026-04-20\n",
    )


# ---------------------------------------------------------------------------
# --strict-runs (Phase 1A)
# ---------------------------------------------------------------------------


def _citation_issues(result: Any) -> list[Any]:
    """Filter to just the citation-existence codes (broken_run + empty_batch)."""
    return [
        i
        for i in result.issues
        if i.code in ("finding.broken_run_citation", "finding.empty_batch")
    ]


@pytest.mark.parametrize(
    "strict_runs,expected_severity,expected_count",
    [
        ("error", "error", 1),
        ("warn", "warning", 1),
        ("off", None, 0),
    ],
)
def test_strict_runs_controls_broken_citation_severity(
    installed_repo: Path,
    strict_runs: str,
    expected_severity: str | None,
    expected_count: int,
) -> None:
    """A missing-job citation is error/warning/skipped per --strict-runs."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    _write_finding_citing_missing_job(kb)

    result = validate_repo(installed_repo, mode="runs-only", strict_runs=strict_runs)
    citation = _citation_issues(result)

    assert len(citation) == expected_count, [i.message for i in citation]
    if expected_severity is not None:
        assert all(i.severity == expected_severity for i in citation), [
            (i.code, i.severity) for i in citation
        ]
        assert all(i.code == "finding.broken_run_citation" for i in citation)


@pytest.mark.parametrize(
    "strict_runs,expected_severity,expected_count",
    [
        ("error", "error", 1),
        ("warn", "warning", 1),
        ("off", None, 0),
    ],
)
def test_strict_runs_controls_empty_batch_severity(
    installed_repo: Path,
    strict_runs: str,
    expected_severity: str | None,
    expected_count: int,
) -> None:
    """An empty-batch citation is error/warning/skipped per --strict-runs."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    _write_finding_citing_empty_batch(kb)

    result = validate_repo(installed_repo, mode="runs-only", strict_runs=strict_runs)
    citation = _citation_issues(result)

    assert len(citation) == expected_count, [i.message for i in citation]
    if expected_severity is not None:
        assert all(i.severity == expected_severity for i in citation), [
            (i.code, i.severity) for i in citation
        ]
        assert all(i.code == "finding.empty_batch" for i in citation)


def test_strict_runs_warn_keeps_validator_ok(installed_repo: Path) -> None:
    """`warn` mode downgrades existence errors so result.ok stays True."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    _write_finding_citing_missing_job(kb)

    error_result = validate_repo(installed_repo, mode="runs-only", strict_runs="error")
    warn_result = validate_repo(installed_repo, mode="runs-only", strict_runs="warn")

    assert not error_result.ok, "default error mode should fail on broken citation"
    assert warn_result.ok, "warn mode should keep result.ok True"
    assert len(warn_result.warnings) >= 1


def test_strict_runs_off_keeps_structural_checks(installed_repo: Path) -> None:
    """`off` skips existence checks but still rejects malformed citations."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    # Malformed: id is not 32-hex. Structural check should still fire.
    _write_artifact(
        kb,
        "research/findings",
        "F001-a.md",
        {
            "id": "F001",
            "type": "finding",
            "hypothesis": "H001",
            "experiment": "E001",
            "impact": "moderate",
            "created": "2026-04-20",
            "supporting_runs": [{"type": "job", "id": "not-32-hex"}],
        },
        "# F001\n> **Hypothesis**: [[H001]]\n> **Experiment**: [[E001]]\n> **Impact**: moderate\n> **Created**: 2026-04-20\n",
    )
    result = validate_repo(installed_repo, mode="runs-only", strict_runs="off")
    citation = _citation_issues(result)
    # Structural id-format check emits a broken_run_citation error even at off
    assert any(
        i.code == "finding.broken_run_citation" and i.severity == "error"
        for i in citation
    ), [(i.code, i.severity, i.message) for i in citation]


def test_strict_runs_default_is_error(installed_repo: Path) -> None:
    """Omitting strict_runs preserves pre-0.6 behavior (error)."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    _write_finding_citing_missing_job(kb)

    result = validate_repo(installed_repo, mode="runs-only")
    citation = _citation_issues(result)

    assert citation, "default mode should still emit the citation issue"
    assert all(i.severity == "error" for i in citation)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_strict_runs_invalid_value(installed_repo: Path) -> None:
    """Invalid --strict-runs values produce exit 2."""
    from typer.testing import CliRunner

    from aexp.cli import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["validate", "--strict-runs", "bogus"],
        env={"AEXP_REPO_ROOT": str(installed_repo)},
    )
    # Typer's _exit(2) on validation error
    assert result.exit_code == 2, result.output
