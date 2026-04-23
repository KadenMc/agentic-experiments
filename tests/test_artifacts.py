"""Tests for H/E/F artifact creation + backlink patching."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aexp.artifacts import (
    ArtifactCreateError,
    new_experiment,
    new_finding,
    new_hypothesis,
    next_artifact_id,
    slugify,
)
from aexp.backlinks import add_backlink
from aexp.install import install_limina
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
    install_limina(repo)
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
