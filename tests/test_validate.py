"""Tests for ``validate_repo`` — composed KB + run-link + finding-citation validation."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aexp.install import install_scaffold
from aexp.runs import create_run
from aexp.validate import VALID_STATUSES, validate_repo


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
    """Write a minimally-conforming kb/ artifact (aliases + Links section).

    kb_validate requires an ``aliases`` frontmatter entry matching the id and
    a ``## Links`` section listing wikilinks to related artifacts; without
    those, even a structurally correct artifact will be rejected.
    """
    import yaml

    fm = dict(frontmatter)
    aid = fm.get("id")
    if aid and "aliases" not in fm:
        fm["aliases"] = [aid]
    target = kb_root / subdir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(fm, sort_keys=False).strip()

    trailing = body

    # Auto-inject required template headers if absent — kb_validate's
    # missing_template_header check (per Kaden's "stick to the templates
    # precisely" directive) requires every shipped H2 to be present.
    artifact_type = fm.get("type", "")
    kind = {"hypothesis": "H", "experiment": "E", "finding": "F"}.get(
        artifact_type
    )
    if kind:
        from aexp.kb_validate import _required_headers_for_kind

        for header in _required_headers_for_kind(kind):
            marker = f"## {header}"
            if marker not in trailing:
                trailing = (
                    trailing.rstrip()
                    + f"\n\n{marker}\n\n_(placeholder for tests.)_\n"
                )

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


# ---------------------------------------------------------------------------
# Clean path
# ---------------------------------------------------------------------------


def test_validate_clean_install_ok(installed_repo: Path) -> None:
    result = validate_repo(installed_repo)
    assert result.ok, [i.message for i in result.errors]


def test_validate_clean_install_with_hypothesis_and_run(installed_repo: Path) -> None:
    kb = installed_repo / "kb"
    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-smoke.md",
        {"id": "H001", "type": "hypothesis", "status": "PROPOSED", "created": "2026-04-20"},
        "# H001 — Smoke\n\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n",
        links=["E001", "ACTIVE", "CHALLENGE"],
    )
    _write_artifact(
        kb,
        "research/experiments",
        "E001-smoke.md",
        {
            "id": "E001",
            "type": "experiment",
            "status": "DESIGNED",
            "hypothesis": "H001",
            "created": "2026-04-20",
        },
        "# E001 — Smoke\n\n> **Status**: DESIGNED\n> **Hypothesis**: [[H001]]\n> **Created**: 2026-04-20\n",
        links=["H001", "ACTIVE", "CHALLENGE"],
    )
    create_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    result = validate_repo(installed_repo)
    assert result.ok, [i.message for i in result.errors]


# ---------------------------------------------------------------------------
# Required-template-header check
# ---------------------------------------------------------------------------


def test_validate_flags_missing_template_header_on_experiment(
    installed_repo: Path,
) -> None:
    """Per Kaden's directive 'stick to templates precisely', the validator
    must catch authors who delete a shipped template section.

    Calls ``kb_validate.validate_kb`` directly because the per-check codes
    (``missing_template_header`` etc.) live there; ``validate_repo`` flattens
    the inner kb-validate output into a single ``aexp.validation_failed``
    issue with the formatted text bundled into the message.
    """
    from aexp.kb_validate import validate_kb

    kb = installed_repo / "kb"
    # H001 fully populated (auto-injected via _write_artifact).
    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-smoke.md",
        {"id": "H001", "type": "hypothesis", "status": "PROPOSED", "created": "2026-04-20"},
        "# H001 — Smoke\n\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n",
        links=["E001", "ACTIVE", "CHALLENGE"],
    )
    # E001 deliberately omits ## Caveats. We construct the body inline so
    # the auto-injection in _write_artifact doesn't add it for us.
    fm_text = (
        "---\n"
        'id: "E001"\n'
        'aliases: ["E001"]\n'
        "type: experiment\n"
        "status: DESIGNED\n"
        'hypothesis: "H001"\n'
        'created: "2026-04-20"\n'
        "---\n"
    )
    body = (
        "# E001 — Smoke\n\n"
        "> **Status**: DESIGNED\n"
        "> **Hypothesis**: [[H001]]\n"
        "> **Created**: 2026-04-20\n\n"
        "## Objective\n\n_x_\n\n"
        "## Setup\n\n_x_\n\n"
        "## Procedure\n\n_x_\n\n"
        # ## Caveats deliberately omitted.
        "## Intent\n\n_x_\n\n"
        "## Progress\n\n_x_\n\n"
        "## Results\n\n_x_\n\n"
        "## Outcome Summary\n\n_x_\n\n"
        "## Decision\n\n_x_\n\n"
        "## Links\n\n- [[H001]]\n- [[ACTIVE]]\n- [[CHALLENGE]]\n"
    )
    target = kb / "research" / "experiments" / "E001-smoke.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fm_text + "\n" + body, encoding="utf-8")

    inner = validate_kb(kb)
    assert not inner.ok
    header_errors = [
        e for e in inner.errors if e.get("check") == "missing_template_header"
    ]
    assert any("Caveats" in e["message"] for e in header_errors), [
        e["message"] for e in inner.errors
    ]


def test_validate_repo_surfaces_missing_template_header_in_bundled_message(
    installed_repo: Path,
) -> None:
    """The outer ``validate_repo`` doesn't preserve per-check codes, but its
    bundled ``aexp.validation_failed`` message must still mention the
    missing header so users see what to fix."""
    kb = installed_repo / "kb"
    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-smoke.md",
        {"id": "H001", "type": "hypothesis", "status": "PROPOSED", "created": "2026-04-20"},
        "# H001 — Smoke\n\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n",
        links=["E001", "ACTIVE", "CHALLENGE"],
    )
    fm_text = (
        "---\n"
        'id: "E001"\n'
        'aliases: ["E001"]\n'
        "type: experiment\n"
        "status: DESIGNED\n"
        'hypothesis: "H001"\n'
        'created: "2026-04-20"\n'
        "---\n"
    )
    body = (
        "# E001 — Smoke\n\n"
        "> **Status**: DESIGNED\n"
        "> **Hypothesis**: [[H001]]\n"
        "> **Created**: 2026-04-20\n\n"
        "## Objective\n\n_x_\n\n"
        "## Setup\n\n_x_\n\n"
        "## Procedure\n\n_x_\n\n"
        "## Intent\n\n_x_\n\n"
        "## Progress\n\n_x_\n\n"
        "## Results\n\n_x_\n\n"
        "## Outcome Summary\n\n_x_\n\n"
        "## Decision\n\n_x_\n\n"
        "## Links\n\n- [[H001]]\n- [[ACTIVE]]\n- [[CHALLENGE]]\n"
    )
    target = kb / "research" / "experiments" / "E001-smoke.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fm_text + "\n" + body, encoding="utf-8")

    result = validate_repo(installed_repo)
    assert not result.ok
    bundled = next(
        i for i in result.errors if i.code == "aexp.validation_failed"
    )
    assert "Caveats" in bundled.message
    assert "missing_template_header" in bundled.message


def test_validate_passes_when_all_template_headers_present(
    installed_repo: Path,
) -> None:
    """The artifacts API renders the full template — its outputs should
    pass the new header check end-to-end."""
    from aexp.artifacts import new_experiment, new_finding, new_hypothesis

    new_hypothesis(title="hypothesis", repo_root=installed_repo, artifact_id="H001")
    new_experiment(
        title="experiment",
        hypothesis_id="H001",
        repo_root=installed_repo,
        artifact_id="E001",
    )
    new_finding(
        title="finding",
        hypothesis_id="H001",
        experiment_id="E001",
        repo_root=installed_repo,
        artifact_id="F001",
    )
    result = validate_repo(installed_repo)
    header_issues = [
        i for i in result.errors if i.code == "missing_template_header"
    ]
    assert header_issues == [], [i.message for i in header_issues]


# ---------------------------------------------------------------------------
# Broken KB
# ---------------------------------------------------------------------------


def test_validate_surfaces_kb_validate_errors(installed_repo: Path) -> None:
    # Drop a malformed experiment that kb_validate will reject.
    bad = installed_repo / "kb" / "research" / "experiments" / "E999-bogus.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not a valid artifact\n", encoding="utf-8")
    result = validate_repo(installed_repo)
    codes = [i.code for i in result.errors]
    assert "aexp.validation_failed" in codes


# ---------------------------------------------------------------------------
# Run-link issues
# ---------------------------------------------------------------------------


def test_validate_flags_orphan_run(installed_repo: Path) -> None:
    # Create a job then wipe its run link so it becomes orphan.
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    del job.doc["aexp"]

    result = validate_repo(installed_repo, mode="runs-only")
    codes = [i.code for i in result.errors]
    assert "run.orphan" in codes


def test_validate_flags_broken_experiment_link(installed_repo: Path) -> None:
    # Reference an experiment that has no file on disk.
    create_run(
        experiment_id="E999",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    result = validate_repo(installed_repo, mode="runs-only")
    codes = [i.code for i in result.errors]
    assert "run.broken_experiment_link" in codes


def test_validate_flags_hypothesis_mismatch(installed_repo: Path) -> None:
    kb = installed_repo / "kb"
    # Experiment frontmatter points at H001; run claims H999 (not in sub-hypotheses).
    _write_artifact(
        kb,
        "research/experiments",
        "E042-mismatch.md",
        {
            "id": "E042",
            "type": "experiment",
            "status": "DESIGNED",
            "hypothesis": "H001",
            "created": "2026-04-20",
        },
        "# E042 — Mismatch\n> **Status**: DESIGNED\n> **Hypothesis**: [[H001]]\n> **Created**: 2026-04-20\n",
    )
    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-a.md",
        {"id": "H001", "type": "hypothesis", "status": "PROPOSED", "created": "2026-04-20"},
        "# H001 — A\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n",
    )
    create_run(
        experiment_id="E042",
        hypothesis_id="H999",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    result = validate_repo(installed_repo, mode="runs-only")
    codes = [i.code for i in result.errors]
    assert "run.hypothesis_mismatch" in codes


def test_validate_flags_invalid_status(installed_repo: Path) -> None:
    job = create_run(
        experiment_id="E001",
        statepoint={"c": "f"},
        repo_root=installed_repo,
    )
    job.doc["status"] = "wut"
    result = validate_repo(installed_repo, mode="runs-only")
    codes = [i.code for i in result.errors]
    assert "run.status_invalid" in codes


# ---------------------------------------------------------------------------
# Finding citations
# ---------------------------------------------------------------------------


def test_validate_hints_when_supporting_runs_is_bare_string(
    installed_repo: Path,
) -> None:
    """A bare string in supporting_runs must be rejected with a helpful hint."""
    kb = installed_repo / "kb"
    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-a.md",
        {"id": "H001", "type": "hypothesis", "status": "PROPOSED", "created": "2026-04-20"},
        "# H001 — A\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n",
        links=["E001", "F001", "ACTIVE", "CHALLENGE"],
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
        links=["H001", "F001", "ACTIVE", "CHALLENGE"],
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
            # Wrong shape: should be a mapping, not a bare string.
            "supporting_runs": ["abcd1234" * 4],
        },
        "# F001 — A\n> **Hypothesis**: [[H001]]\n> **Experiment**: [[E001]]\n> **Impact**: moderate\n> **Created**: 2026-04-20\n",
        links=["H001", "E001", "ACTIVE", "CHALLENGE"],
    )
    result = validate_repo(installed_repo, mode="runs-only")
    matching = [i for i in result.errors if i.code == "finding.broken_run_citation"]
    assert matching, result.errors
    # Hint must mention the two valid mapping shapes.
    msg = matching[0].message
    assert "mapping" in msg
    assert "type: job" in msg
    assert "type: batch" in msg


def test_validate_hints_when_supporting_runs_missing_type(
    installed_repo: Path,
) -> None:
    """A mapping without 'type' key must report with an 'expected type=' hint."""
    kb = installed_repo / "kb"
    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-a.md",
        {"id": "H001", "type": "hypothesis", "status": "PROPOSED", "created": "2026-04-20"},
        "# H001 — A\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n",
        links=["E001", "F002", "ACTIVE", "CHALLENGE"],
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
        links=["H001", "F002", "ACTIVE", "CHALLENGE"],
    )
    _write_artifact(
        kb,
        "research/findings",
        "F002-a.md",
        {
            "id": "F002",
            "type": "finding",
            "hypothesis": "H001",
            "experiment": "E001",
            "impact": "moderate",
            "created": "2026-04-20",
            # Missing 'type' key entirely.
            "supporting_runs": [{"id": "a" * 32}],
        },
        "# F002 — A\n> **Hypothesis**: [[H001]]\n> **Experiment**: [[E001]]\n> **Impact**: moderate\n> **Created**: 2026-04-20\n",
        links=["H001", "E001", "ACTIVE", "CHALLENGE"],
    )
    result = validate_repo(installed_repo, mode="runs-only")
    matching = [i for i in result.errors if i.code == "finding.broken_run_citation"]
    assert matching, result.errors
    msg = matching[0].message
    assert "type='job'" in msg or "type=None" in msg
    assert "'job'" in msg or "'batch'" in msg


def test_validate_flags_broken_run_citation(installed_repo: Path) -> None:
    kb = installed_repo / "kb"
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
    result = validate_repo(installed_repo, mode="runs-only")
    codes = [i.code for i in result.errors]
    assert "finding.broken_run_citation" in codes


def test_validate_flags_empty_batch_citation(installed_repo: Path) -> None:
    kb = installed_repo / "kb"
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
    result = validate_repo(installed_repo, mode="runs-only")
    codes = [i.code for i in result.errors]
    assert "finding.empty_batch" in codes


def test_valid_statuses_constant_matches_run_status_literal() -> None:
    # ``stopped`` joined the literal in 0.2.1 (operator-stop terminal status,
    # set by `aexp queue stop <jobid>`); ``queued`` is intentionally excluded
    # from VALID_STATUSES because the validator runs against post-hoc /
    # finished runs, not pending entries.
    assert VALID_STATUSES == {
        "created",
        "running",
        "complete",
        "failed",
        "abandoned",
        "stopped",
    }


def test_mode_kb_only_skips_run_checks(installed_repo: Path) -> None:
    # Create an orphan run; kb-only mode should ignore it.
    job = create_run(
        experiment_id="E001", statepoint={"c": "f"}, repo_root=installed_repo
    )
    del job.doc["aexp"]
    result = validate_repo(installed_repo, mode="kb-only")
    # Might still fail on kb_validate (e.g. if the orphan kb/ wasn't touched),
    # but no run.* codes should appear.
    assert not any(i.code.startswith("run.") for i in result.issues)


def test_mode_runs_only_skips_kb_validate(installed_repo: Path) -> None:
    # Break the KB with a malformed artifact; runs-only mode should not report it.
    bad = installed_repo / "kb" / "research" / "experiments" / "E999-bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("garbage\n", encoding="utf-8")
    result = validate_repo(installed_repo, mode="runs-only")
    codes = [i.code for i in result.issues]
    assert "aexp.validation_failed" not in codes
