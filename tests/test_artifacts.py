"""Tests for H/E/F artifact creation + backlink patching."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aexp.artifacts import (
    ArtifactCreateError,
    ThreadStatusUpdate,
    close_thread,
    new_experiment,
    new_finding,
    new_hypothesis,
    new_thread,
    next_artifact_id,
    slugify,
)
from aexp.backlinks import add_backlink
from aexp.install import install_scaffold
from aexp.kb_validate import validate_kb


def _git_commit(repo: Path, msg: str = "seed") -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture_output=True
    )
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", msg],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def installed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_commit(repo)
    install_scaffold(repo)
    return repo


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


def test_slugify_basic() -> None:
    assert slugify("Aligned 32-ECG ablation") == "aligned-32-ecg-ablation"


def test_slugify_punctuation_stripped() -> None:
    assert slugify("Full vs. classify_only — verdict!") == "full-vs-classify-only-verdict"


def test_slugify_empty_falls_back() -> None:
    assert slugify("   !!!  ") == "untitled"


def test_slugify_truncates_long_titles() -> None:
    title = "a" * 200
    assert len(slugify(title)) <= 60


# ---------------------------------------------------------------------------
# next_artifact_id
# ---------------------------------------------------------------------------


def test_next_artifact_id_starts_at_001(installed_repo: Path) -> None:
    kb = installed_repo / "kb"
    assert next_artifact_id("H", kb_root=kb) == "H001"
    assert next_artifact_id("E", kb_root=kb) == "E001"
    assert next_artifact_id("F", kb_root=kb) == "F001"


def test_next_artifact_id_fills_smallest_gap(installed_repo: Path) -> None:
    kb = installed_repo / "kb"
    new_hypothesis(title="first", repo_root=installed_repo)
    new_hypothesis(title="second", repo_root=installed_repo)
    # Delete H001 to create a gap.
    h001 = next(
        (kb / "research" / "hypotheses").glob("H001-*.md")
    )
    h001.unlink()
    assert next_artifact_id("H", kb_root=kb) == "H001"


# ---------------------------------------------------------------------------
# new_hypothesis
# ---------------------------------------------------------------------------


def test_new_hypothesis_creates_file(installed_repo: Path) -> None:
    result = new_hypothesis(title="Test claim", repo_root=installed_repo)
    assert result.artifact_id == "H001"
    assert result.path == "kb/research/hypotheses/H001-test-claim.md"
    assert (installed_repo / result.path).is_file()


def test_new_hypothesis_content_is_validator_clean(installed_repo: Path) -> None:
    new_hypothesis(title="A falsifiable claim", repo_root=installed_repo)
    path = installed_repo / "kb" / "research" / "hypotheses" / "H001-a-falsifiable-claim.md"
    text = path.read_text(encoding="utf-8")
    assert 'id: "H001"' in text
    assert 'aliases: ["H001"]' in text
    assert "# H001 — A falsifiable claim" in text
    assert "## Links" in text
    assert "- [[ACTIVE]]" in text
    assert "- [[CHALLENGE]]" in text
    # Kaden's lowercase "{command}" placeholders in the template must NOT be
    # mangled by the renderer.
    assert "{command}" in text


def test_new_hypothesis_rejects_empty_title(installed_repo: Path) -> None:
    with pytest.raises(ArtifactCreateError):
        new_hypothesis(title="   ", repo_root=installed_repo)


def test_new_hypothesis_accepts_explicit_id(installed_repo: Path) -> None:
    result = new_hypothesis(
        title="Third", repo_root=installed_repo, artifact_id="H007"
    )
    assert result.artifact_id == "H007"


def test_new_hypothesis_refuses_duplicate_id(installed_repo: Path) -> None:
    new_hypothesis(title="first", repo_root=installed_repo, artifact_id="H001")
    with pytest.raises(ArtifactCreateError):
        new_hypothesis(title="dup", repo_root=installed_repo, artifact_id="H001")


# ---------------------------------------------------------------------------
# new_experiment
# ---------------------------------------------------------------------------


def test_new_experiment_requires_existing_hypothesis(installed_repo: Path) -> None:
    with pytest.raises(ArtifactCreateError):
        new_experiment(
            title="x", hypothesis_id="H099", repo_root=installed_repo
        )


def test_new_experiment_rejects_malformed_hypothesis_id(installed_repo: Path) -> None:
    with pytest.raises(ArtifactCreateError):
        new_experiment(
            title="x", hypothesis_id="bogus", repo_root=installed_repo
        )


def test_new_experiment_patches_parent_hypothesis(installed_repo: Path) -> None:
    new_hypothesis(title="root", repo_root=installed_repo)
    result = new_experiment(
        title="sub", hypothesis_id="H001", repo_root=installed_repo
    )
    assert result.artifact_id == "E001"
    parent = installed_repo / "kb" / "research" / "hypotheses" / "H001-root.md"
    assert "[[E001]]" in parent.read_text(encoding="utf-8")
    assert any("H001-root.md" in p for p in result.backlinks_patched)


# ---------------------------------------------------------------------------
# new_finding
# ---------------------------------------------------------------------------


def test_new_finding_patches_both_parents(installed_repo: Path) -> None:
    new_hypothesis(title="h", repo_root=installed_repo)
    new_experiment(title="e", hypothesis_id="H001", repo_root=installed_repo)
    result = new_finding(
        title="f",
        hypothesis_id="H001",
        experiment_id="E001",
        impact="HIGH",
        repo_root=installed_repo,
    )
    assert result.artifact_id == "F001"
    h = (installed_repo / "kb" / "research" / "hypotheses" / "H001-h.md").read_text(
        encoding="utf-8"
    )
    e = (installed_repo / "kb" / "research" / "experiments" / "E001-e.md").read_text(
        encoding="utf-8"
    )
    assert "[[F001]]" in h
    assert "[[F001]]" in e
    assert len(result.backlinks_patched) == 2


def test_new_finding_records_impact(installed_repo: Path) -> None:
    new_hypothesis(title="h", repo_root=installed_repo)
    new_experiment(title="e", hypothesis_id="H001", repo_root=installed_repo)
    new_finding(
        title="f",
        hypothesis_id="H001",
        experiment_id="E001",
        impact="CRITICAL",
        repo_root=installed_repo,
    )
    finding = (
        installed_repo / "kb" / "research" / "findings" / "F001-f.md"
    ).read_text(encoding="utf-8")
    assert 'impact: "CRITICAL"' in finding


def test_new_finding_missing_parent_errors(installed_repo: Path) -> None:
    new_hypothesis(title="h", repo_root=installed_repo)
    with pytest.raises(ArtifactCreateError):
        new_finding(
            title="f",
            hypothesis_id="H001",
            experiment_id="E099",
            repo_root=installed_repo,
        )


# ---------------------------------------------------------------------------
# End-to-end: H -> E -> F passes validate_kb clean
# ---------------------------------------------------------------------------


def test_full_chain_validates_clean(installed_repo: Path) -> None:
    new_hypothesis(title="claim", repo_root=installed_repo)
    new_experiment(title="design", hypothesis_id="H001", repo_root=installed_repo)
    new_finding(
        title="verdict",
        hypothesis_id="H001",
        experiment_id="E001",
        repo_root=installed_repo,
    )
    result = validate_kb(installed_repo / "kb")
    assert result.ok, f"validation failed: {result.errors}"


# ---------------------------------------------------------------------------
# Single source of truth: artifact creation reads the bundled templates only
# ---------------------------------------------------------------------------


def test_new_hypothesis_ignores_local_template_override(
    installed_repo: Path,
) -> None:
    """Regression guard for the templates-source-of-truth fix.

    When a consumer's local ``templates/<kind>.md`` is stale (or
    customised), the artifact-creation API must still read from the
    bundled template — same source the validator uses. Otherwise
    creation produces a skeleton that immediately fails validation.

    A 2026-04-24 consumer report described exactly this failure:
    install-preserve correctly kept stale local templates, but
    ``new_experiment`` then rendered the old shape while the validator
    expected the new one.
    """
    # Stuff the local hypothesis template with bogus content that has
    # none of the headers the bundled template ships.
    local = installed_repo / "templates" / "hypothesis.md"
    local.write_text(
        '---\nid: "{ARTIFACT_ID}"\naliases: ["{ARTIFACT_ID}"]\n'
        'type: hypothesis\nstatus: PROPOSED\ncreated: "{DATE}"\n'
        'last_updated: "{DATE}"\ntags: []\n---\n\n'
        "# {ARTIFACT_ID} — {TITLE}\n\n"
        "## OnlyMe\n\nbogus stale content\n\n"
        "## Links\n\n{LINKS_BLOCK}\n",
        encoding="utf-8",
    )
    new_hypothesis(title="t", repo_root=installed_repo)
    rendered = (
        installed_repo / "kb" / "research" / "hypotheses" / "H001-t.md"
    ).read_text(encoding="utf-8")
    # The bundled template's headers must be present.
    assert "## Statement" in rendered
    assert "## Test Plan" in rendered
    assert "## Conclusion" in rendered
    # The bogus local override must NOT have leaked into the rendered file.
    assert "## OnlyMe" not in rendered
    assert "bogus stale content" not in rendered


def test_new_experiment_skeleton_passes_validator_unmodified(
    installed_repo: Path,
) -> None:
    """End-to-end regression guard: a freshly-rendered experiment skeleton
    must satisfy the required-template-header validator without any
    post-creation edits. Asserts the creation source and validation
    source agree.
    """
    from aexp.kb_validate import validate_kb

    new_hypothesis(title="h", repo_root=installed_repo)
    new_experiment(
        title="e", hypothesis_id="H001", repo_root=installed_repo
    )
    result = validate_kb(installed_repo / "kb")
    header_errors = [
        e for e in result.errors if e.get("check") == "missing_template_header"
    ]
    assert header_errors == [], result.errors


def test_new_finding_skeleton_passes_validator_unmodified(
    installed_repo: Path,
) -> None:
    """Same regression guard for findings — the new ``## Caveats``
    section must be in the rendered skeleton, not just the validator's
    expectation."""
    from aexp.kb_validate import validate_kb

    new_hypothesis(title="h", repo_root=installed_repo)
    new_experiment(
        title="e", hypothesis_id="H001", repo_root=installed_repo
    )
    new_finding(
        title="f",
        hypothesis_id="H001",
        experiment_id="E001",
        repo_root=installed_repo,
    )
    result = validate_kb(installed_repo / "kb")
    header_errors = [
        e for e in result.errors if e.get("check") == "missing_template_header"
    ]
    assert header_errors == [], result.errors


# ---------------------------------------------------------------------------
# Threads (T###)
# ---------------------------------------------------------------------------


def test_new_thread_creates_validator_clean_skeleton(installed_repo: Path) -> None:
    result = new_thread(title="hierarchy-aware scoring", repo_root=installed_repo)
    assert result.artifact_id == "T001"
    assert result.path.endswith("/T001-hierarchy-aware-scoring.md")
    text = (installed_repo / result.path).read_text(encoding="utf-8")
    assert 'id: "T001"' in text
    assert 'aliases: ["T001"]' in text
    assert 'status: PROPOSED' in text
    # Must include all required template headers.
    for header in (
        "## Statement",
        "## Sub-questions",
        "## Promotion criteria",
        "## Open links",
        "## Notes",
        "## Conclusion",
        "## Links",
    ):
        assert header in text, header

    inner = validate_kb(installed_repo / "kb")
    header_errors = [
        e for e in inner.errors if e.get("check") == "missing_template_header"
    ]
    assert header_errors == [], inner.errors


def test_next_artifact_id_T_starts_at_001(installed_repo: Path) -> None:
    kb = installed_repo / "kb"
    assert next_artifact_id("T", kb_root=kb) == "T001"


def test_new_hypothesis_with_thread_id_patches_thread_links(
    installed_repo: Path,
) -> None:
    new_thread(title="t", repo_root=installed_repo, artifact_id="T001")
    result = new_hypothesis(
        title="h",
        thread_id="T001",
        repo_root=installed_repo,
        artifact_id="H001",
    )
    assert result.artifact_id == "H001"
    # Thread's ## Links should now include [[H001]] (auto-patched).
    thread_path = installed_repo / "kb" / "research" / "threads" / "T001-t.md"
    assert "[[H001]]" in thread_path.read_text(encoding="utf-8")
    # Hypothesis frontmatter records the thread.
    h_path = installed_repo / "kb" / "research" / "hypotheses" / "H001-h.md"
    assert 'thread: "T001"' in h_path.read_text(encoding="utf-8")
    # Backlinks list shows the thread file.
    assert any("T001" in p for p in result.backlinks_patched)


def test_new_hypothesis_rejects_unknown_thread_id(installed_repo: Path) -> None:
    with pytest.raises(ArtifactCreateError):
        new_hypothesis(
            title="h", thread_id="T099", repo_root=installed_repo
        )


def test_new_hypothesis_rejects_malformed_thread_id(installed_repo: Path) -> None:
    new_thread(title="t", repo_root=installed_repo, artifact_id="T001")
    with pytest.raises(ArtifactCreateError):
        new_hypothesis(
            title="h", thread_id="bogus", repo_root=installed_repo
        )


def test_new_hypothesis_without_thread_leaves_thread_field_empty(
    installed_repo: Path,
) -> None:
    new_hypothesis(title="h", repo_root=installed_repo, artifact_id="H001")
    text = (
        installed_repo / "kb" / "research" / "hypotheses" / "H001-h.md"
    ).read_text(encoding="utf-8")
    assert 'thread: ""' in text


def test_h_e_f_chain_validates_clean_with_thread(installed_repo: Path) -> None:
    """Full chain T → H → E → F should pass validate_kb."""
    new_thread(title="root", repo_root=installed_repo, artifact_id="T001")
    new_hypothesis(
        title="h",
        thread_id="T001",
        repo_root=installed_repo,
        artifact_id="H001",
    )
    new_experiment(
        title="e",
        hypothesis_id="H001",
        repo_root=installed_repo,
        artifact_id="E001",
    )
    new_finding(
        title="f",
        hypothesis_id="H001",
        experiment_id="E001",
        repo_root=installed_repo,
        artifact_id="F001",
    )
    inner = validate_kb(installed_repo / "kb")
    assert inner.ok, inner.errors


def test_close_thread_transitions_status_and_writes_conclusion(
    installed_repo: Path,
) -> None:
    new_thread(title="t", repo_root=installed_repo, artifact_id="T001")
    update = close_thread(
        "T001",
        conclusion="Decided not to pursue — superseded by Thread A.",
        repo_root=installed_repo,
    )
    assert isinstance(update, ThreadStatusUpdate)
    assert update.new_status == "CLOSED"
    assert update.conclusion_written is True

    text = (
        installed_repo / "kb" / "research" / "threads" / "T001-t.md"
    ).read_text(encoding="utf-8")
    assert "status: CLOSED" in text
    assert "Decided not to pursue" in text


def test_close_thread_promoted_status(installed_repo: Path) -> None:
    new_thread(title="t", repo_root=installed_repo, artifact_id="T001")
    update = close_thread(
        "T001",
        conclusion="Spawned [[H001]]; thread persists as parent context.",
        new_status="PROMOTED",
        repo_root=installed_repo,
    )
    assert update.new_status == "PROMOTED"
    text = (
        installed_repo / "kb" / "research" / "threads" / "T001-t.md"
    ).read_text(encoding="utf-8")
    assert "status: PROMOTED" in text


def test_close_thread_unknown_id_errors(installed_repo: Path) -> None:
    with pytest.raises(ArtifactCreateError):
        close_thread("T099", repo_root=installed_repo)


def test_close_thread_rejects_invalid_status(installed_repo: Path) -> None:
    new_thread(title="t", repo_root=installed_repo, artifact_id="T001")
    with pytest.raises(ArtifactCreateError):
        close_thread(
            "T001", new_status="GARBAGE", repo_root=installed_repo
        )


# ---------------------------------------------------------------------------
# backlink helper
# ---------------------------------------------------------------------------


def test_add_backlink_appends_to_existing_section(tmp_path: Path) -> None:
    md = tmp_path / "parent.md"
    md.write_text(
        "# Parent\n\nbody\n\n## Links\n\n- [[ACTIVE]]\n- [[CHALLENGE]]\n",
        encoding="utf-8",
    )
    changed = add_backlink(md, "F001")
    assert changed is True
    text = md.read_text(encoding="utf-8")
    assert "- [[F001]]" in text
    assert text.count("## Links") == 1
    # Ordering: new link appended after existing bullets, before EOF/next heading.
    assert text.index("[[CHALLENGE]]") < text.index("[[F001]]")


def test_add_backlink_is_idempotent(tmp_path: Path) -> None:
    md = tmp_path / "parent.md"
    md.write_text(
        "# Parent\n\n## Links\n\n- [[F001]]\n- [[ACTIVE]]\n",
        encoding="utf-8",
    )
    changed = add_backlink(md, "F001")
    assert changed is False


def test_add_backlink_creates_section_if_missing(tmp_path: Path) -> None:
    md = tmp_path / "parent.md"
    md.write_text("# Parent\n\nbody only\n", encoding="utf-8")
    changed = add_backlink(md, "F001")
    assert changed is True
    text = md.read_text(encoding="utf-8")
    assert "## Links" in text
    assert "- [[F001]]" in text


def test_add_backlink_handles_anchor_and_alias_forms(tmp_path: Path) -> None:
    md = tmp_path / "parent.md"
    md.write_text(
        "# Parent\n\n## Links\n\n- [[F001#section]]\n",
        encoding="utf-8",
    )
    # Should see the anchored link as already present.
    assert add_backlink(md, "F001") is False


def test_add_backlink_stops_at_next_heading(tmp_path: Path) -> None:
    md = tmp_path / "parent.md"
    md.write_text(
        "# Parent\n\n## Links\n\n- [[ACTIVE]]\n\n## Notes\n\nsome notes\n",
        encoding="utf-8",
    )
    add_backlink(md, "F001")
    text = md.read_text(encoding="utf-8")
    # Inserted before ## Notes, not after.
    assert text.index("[[F001]]") < text.index("## Notes")
