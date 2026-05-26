"""Tests for the cross-machine validation features.

Covers ``--strict-runs={error|warn|off}`` (Phase 1A) and — once Phases 1B and 2
land — the per-machine index three-state vocabulary and the ledger-source
switch.

The test fixtures here mirror the broken-citation + empty-batch shapes from
``test_validate.py`` so we exercise the same code paths from the citation
check, just with the new severity policy in play.
"""
from __future__ import annotations

import json as _json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from aexp.install import install_scaffold
from aexp.runs_index import (
    INDEX_DIR_REL,
    SCHEMA_VERSION,
    build_index,
    collect_known_elsewhere,
    export_index,
    load_all_indexes,
)
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


# ---------------------------------------------------------------------------
# Phase 1B — per-machine runs-index files (three-state vocabulary)
# ---------------------------------------------------------------------------


def _write_index_file(
    repo: Path,
    machine_label: str,
    entries: list[dict],
) -> Path:
    """Helper: write a synthetic index file for a foreign machine."""
    target = repo / INDEX_DIR_REL / f"{machine_label}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "machine_label": machine_label,
        "exported_at": "2026-05-26T00:00:00Z",
        "entries": entries,
    }
    target.write_text(_json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def test_finding_citing_elsewhere_job_emits_absent_warning(
    installed_repo: Path,
) -> None:
    """Citing a job present in another machine's index → absent_run_citation (warning)."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    far_job_id = "a" * 32
    _write_index_file(
        installed_repo,
        "cluster",
        [
            {
                "job_id": far_job_id,
                "experiment_id": "E001",
                "status": "complete",
            }
        ],
    )
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
            "supporting_runs": [{"type": "job", "id": far_job_id}],
        },
        "# F001\n> **Hypothesis**: [[H001]]\n> **Experiment**: [[E001]]\n> **Impact**: moderate\n> **Created**: 2026-04-20\n",
    )

    result = validate_repo(installed_repo, mode="runs-only")
    absent = [i for i in result.issues if i.code == "finding.absent_run_citation"]
    broken = [i for i in result.issues if i.code == "finding.broken_run_citation"]

    assert len(absent) == 1, [i.message for i in result.issues]
    assert absent[0].severity == "warning"
    assert "cluster" in absent[0].message
    assert broken == [], [i.message for i in broken]
    # Validator stays OK (warnings don't fail)
    assert result.ok


def test_finding_citing_nowhere_job_still_emits_broken(
    installed_repo: Path,
) -> None:
    """Citing a job that's neither local nor in any index → broken_run_citation (error)."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    # An index that doesn't include the cited job
    _write_index_file(
        installed_repo,
        "cluster",
        [
            {
                "job_id": "a" * 32,
                "experiment_id": "E001",
                "status": "complete",
            }
        ],
    )
    # The finding cites a DIFFERENT job (f*32, not a*32)
    _write_finding_citing_missing_job(kb)

    result = validate_repo(installed_repo, mode="runs-only")
    broken = [i for i in result.issues if i.code == "finding.broken_run_citation"]
    absent = [i for i in result.issues if i.code == "finding.absent_run_citation"]

    assert len(broken) == 1
    assert broken[0].severity == "error"
    assert absent == []


def test_batch_only_elsewhere_emits_absent_batch_warning(
    installed_repo: Path,
) -> None:
    """Batch citation matching only elsewhere-indexed runs → absent_batch_runs (warning)."""
    kb = installed_repo / "kb"
    _seed_h_e_artifacts(kb)
    _write_index_file(
        installed_repo,
        "cluster",
        [
            {
                "job_id": "b" * 32,
                "experiment_id": "E001",
                "condition": "ghost",
                "status": "complete",
            }
        ],
    )
    _write_finding_citing_empty_batch(kb)

    result = validate_repo(installed_repo, mode="runs-only")
    absent = [i for i in result.issues if i.code == "finding.absent_batch_runs"]
    empty = [i for i in result.issues if i.code == "finding.empty_batch"]

    assert len(absent) == 1, [i.code for i in result.issues]
    assert absent[0].severity == "warning"
    assert "cluster" in absent[0].message
    assert empty == []


def test_no_run_store_emits_warning_once(tmp_path: Path) -> None:
    """When neither store nor index exists, emit finding.no_run_store (warning)."""
    # Set up a bare repo with kb/ but no .runs/ and no .aexp/runs-index/.
    repo = tmp_path / "bare"
    repo.mkdir()
    _git_commit(repo)
    install_scaffold(repo)
    # Now delete the run store + index dir entirely (simulating a checkout
    # on a brand-new machine before any runs/exports).
    import shutil

    shutil.rmtree(repo / ".runs", ignore_errors=True)
    shutil.rmtree(repo / ".aexp" / "runs-index", ignore_errors=True)

    kb = repo / "kb"
    _seed_h_e_artifacts(kb)
    _write_finding_citing_missing_job(kb)

    result = validate_repo(repo, mode="runs-only")
    no_store = [i for i in result.issues if i.code == "finding.no_run_store"]
    broken = [i for i in result.issues if i.code == "finding.broken_run_citation"]

    # Exactly one no_run_store warning (per run, not per citation)
    assert len(no_store) == 1
    assert no_store[0].severity == "warning"
    # No broken_run_citation when there's nothing to compare against
    assert broken == []


# ---------------------------------------------------------------------------
# runs_index module
# ---------------------------------------------------------------------------


def test_build_index_skips_non_terminal_jobs(installed_repo: Path) -> None:
    """Only complete/failed/abandoned/stopped jobs land in the index."""
    from aexp.runs import create_run, mark_status

    # Create a run that stays in `created` status — should be skipped.
    pending = create_run(
        experiment_id="E001",
        statepoint={"c": "pending"},
        repo_root=installed_repo,
    )
    # Create + complete a run — should appear.
    done = create_run(
        experiment_id="E001",
        statepoint={"c": "done"},
        repo_root=installed_repo,
    )
    mark_status(done, "complete")

    index = build_index(installed_repo)
    ids = [e["job_id"] for e in index["entries"]]
    assert done.id in ids
    assert pending.id not in ids
    assert index["schema_version"] == SCHEMA_VERSION
    assert "machine_label" in index
    assert "exported_at" in index


def test_export_index_writes_default_path(installed_repo: Path) -> None:
    """Default path is .aexp/runs-index/<machine_label>.json."""
    from aexp.utils.paths import read_machine_label

    path = export_index(installed_repo)
    expected_label = read_machine_label(installed_repo)
    assert path == installed_repo / INDEX_DIR_REL / f"{expected_label}.json"
    assert path.is_file()


def test_export_index_with_machine_label_override(installed_repo: Path) -> None:
    """--machine-label override sets both filename and body field."""
    path = export_index(installed_repo, machine_label="cluster")
    assert path.name == "cluster.json"
    data = _json.loads(path.read_text(encoding="utf-8"))
    assert data["machine_label"] == "cluster"


def test_load_all_indexes_skips_malformed_files(installed_repo: Path) -> None:
    """Malformed index files don't break the validator load path."""
    # Write a valid index
    _write_index_file(installed_repo, "good", [{"job_id": "a" * 32}])
    # Write a malformed one (not JSON)
    bad = installed_repo / INDEX_DIR_REL / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not valid json {{{", encoding="utf-8")
    # Write a JSON that's missing the entries key
    missing_key = installed_repo / INDEX_DIR_REL / "weird.json"
    missing_key.write_text(_json.dumps({"unrelated": "data"}), encoding="utf-8")

    indexes = load_all_indexes(installed_repo)
    assert "good" in indexes
    assert "bad" not in indexes
    assert "weird" not in indexes


def test_collect_known_elsewhere_tags_ledger_machine(installed_repo: Path) -> None:
    """Each elsewhere-entry gets the ledger_machine field set."""
    _write_index_file(
        installed_repo,
        "cluster",
        [{"job_id": "a" * 32, "experiment_id": "E001"}],
    )
    _write_index_file(
        installed_repo,
        "another-laptop",
        [{"job_id": "b" * 32, "experiment_id": "E002"}],
    )
    elsewhere = collect_known_elsewhere(installed_repo)
    assert elsewhere["a" * 32]["ledger_machine"] == "cluster"
    assert elsewhere["b" * 32]["ledger_machine"] == "another-laptop"


def test_export_index_byte_stable_on_unchanged_state(installed_repo: Path) -> None:
    """Two exports of the same state produce identical entries.

    The `exported_at` timestamp differs but the entries list is sorted by
    job_id, so a hash of just the entries should be stable.
    """
    from aexp.runs import create_run, mark_status

    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    mark_status(job, "complete")

    p1 = export_index(installed_repo)
    data1 = _json.loads(p1.read_text(encoding="utf-8"))
    p2 = export_index(installed_repo)
    data2 = _json.loads(p2.read_text(encoding="utf-8"))

    assert data1["entries"] == data2["entries"]


# ---------------------------------------------------------------------------
# CLI for runs-export-index
# ---------------------------------------------------------------------------


def test_cli_runs_export_index_writes_file(
    installed_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`aexp runs-export-index` writes an index file and reports success."""
    from typer.testing import CliRunner

    from aexp.cli import app

    monkeypatch.chdir(installed_repo)
    runner = CliRunner()
    result = runner.invoke(app, ["runs-export-index", "--as", "cluster"])
    assert result.exit_code == 0, result.output
    assert "[OK]" in result.output
    out = installed_repo / INDEX_DIR_REL / "cluster.json"
    assert out.is_file()
