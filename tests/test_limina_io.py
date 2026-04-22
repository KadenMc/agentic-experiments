"""Tests for ``limina_io`` artifact readers."""
from __future__ import annotations

from pathlib import Path

import pytest

from aexp.install import install_limina
from aexp.limina_io import (
    ArtifactNotFoundError,
    ArtifactReadError,
    find_artifact_path,
    kind_dir,
    list_kb_artifacts,
    load_artifact,
    load_experiment,
    load_finding,
    load_hypothesis,
    parse_artifact_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_artifact(
    kb_root: Path,
    kind_subdir: str,
    filename: str,
    frontmatter: dict[str, object],
    body: str,
) -> Path:
    """Write a minimal Limina-shaped artifact to disk."""
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            fm_lines.append(f"{k}: {v}")
        elif isinstance(v, str):
            fm_lines.append(f'{k}: "{v}"')
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    target = kb_root / kind_subdir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(fm_lines) + "\n\n" + body, encoding="utf-8")
    return target


@pytest.fixture
def populated_kb(tmp_path: Path) -> Path:
    """A fresh kb/ with one artifact of each kind for read-side tests."""
    install_limina(tmp_path, assert_git=False)
    kb = tmp_path / "kb"

    _write_artifact(
        kb,
        "research/hypotheses",
        "H001-smoke.md",
        {
            "id": "H001",
            "aliases": ["H001"],
            "type": "hypothesis",
            "status": "PROPOSED",
            "created": "2026-04-20",
            "last_updated": "2026-04-20",
        },
        "# H001 — Smoke hypothesis\n\nBody text.\n",
    )
    _write_artifact(
        kb,
        "research/experiments",
        "E001-smoke.md",
        {
            "id": "E001",
            "aliases": ["E001"],
            "type": "experiment",
            "status": "DESIGNED",
            "hypothesis": "H001",
            "created": "2026-04-20",
        },
        "# E001 — Smoke experiment\n\n## Local Hypothesis\n\nWithin full condition...\n",
    )
    _write_artifact(
        kb,
        "research/findings",
        "F001-smoke.md",
        {
            "id": "F001",
            "aliases": ["F001"],
            "type": "finding",
            "hypothesis": "H001",
            "experiment": "E001",
            "impact": "moderate",
            "created": "2026-04-20",
        },
        "# F001 — Smoke finding\n\nVerdict body.\n",
    )
    return kb


# ---------------------------------------------------------------------------
# parse_artifact_id
# ---------------------------------------------------------------------------


def test_parse_artifact_id_basic() -> None:
    assert parse_artifact_id("H001") == ("H", 1)
    assert parse_artifact_id("E018") == ("E", 18)
    assert parse_artifact_id("CR042") == ("CR", 42)
    assert parse_artifact_id("SR007") == ("SR", 7)


def test_parse_artifact_id_rejects_bad() -> None:
    for bad in ("", "H1", "H0001", "X001", "e001", "H-001"):
        with pytest.raises(ValueError):
            parse_artifact_id(bad)


# ---------------------------------------------------------------------------
# find_artifact_path
# ---------------------------------------------------------------------------


def test_find_artifact_path_hypothesis(populated_kb: Path) -> None:
    p = find_artifact_path("H001", kb_root=populated_kb)
    assert p.name == "H001-smoke.md"
    assert p.parent.name == "hypotheses"


def test_find_artifact_path_missing_raises(populated_kb: Path) -> None:
    with pytest.raises(ArtifactNotFoundError):
        find_artifact_path("H999", kb_root=populated_kb)


def test_find_artifact_path_detects_duplicates(populated_kb: Path) -> None:
    # Create a second file with the same id prefix.
    dup = populated_kb / "research" / "hypotheses" / "H001-dup.md"
    dup.write_text("---\nid: H001\n---\n", encoding="utf-8")
    with pytest.raises(ArtifactReadError, match="multiple"):
        find_artifact_path("H001", kb_root=populated_kb)


# ---------------------------------------------------------------------------
# kind_dir
# ---------------------------------------------------------------------------


def test_kind_dir_mapping(populated_kb: Path) -> None:
    assert kind_dir("H", populated_kb).parts[-2:] == ("research", "hypotheses")
    assert kind_dir("E", populated_kb).parts[-2:] == ("research", "experiments")
    assert kind_dir("F", populated_kb).parts[-2:] == ("research", "findings")
    assert kind_dir("L", populated_kb).parts[-2:] == ("research", "literature")
    assert kind_dir("CR", populated_kb).name == "reports"
    assert kind_dir("SR", populated_kb).name == "reports"


# ---------------------------------------------------------------------------
# load_*
# ---------------------------------------------------------------------------


def test_load_hypothesis_returns_typed_ref(populated_kb: Path) -> None:
    ref = load_hypothesis("H001", kb_root=populated_kb)
    assert ref.kind == "H"
    assert ref.id == "H001"
    assert ref.title == "Smoke hypothesis"
    assert ref.metadata["status"] == "PROPOSED"
    assert "Body text" in ref.body


def test_load_experiment_preserves_local_hypothesis_section(populated_kb: Path) -> None:
    ref = load_experiment("E001", kb_root=populated_kb)
    assert "Local Hypothesis" in ref.body
    assert ref.metadata["hypothesis"] == "H001"


def test_load_finding_carries_experiment_link(populated_kb: Path) -> None:
    ref = load_finding("F001", kb_root=populated_kb)
    assert ref.metadata["experiment"] == "E001"
    assert ref.metadata["hypothesis"] == "H001"


def test_load_artifact_rejects_kind_mismatch(populated_kb: Path) -> None:
    # Asking for a hypothesis but pointing at E001 -> mismatch.
    with pytest.raises(ArtifactReadError, match="not H"):
        load_hypothesis("E001", kb_root=populated_kb)


def test_load_artifact_path_is_repo_relative_posix(populated_kb: Path) -> None:
    ref = load_experiment("E001", kb_root=populated_kb)
    assert ref.path.endswith("kb/research/experiments/E001-smoke.md")
    assert "\\" not in ref.path  # POSIX slashes


def test_load_artifact_recovers_id_from_filename(tmp_path: Path) -> None:
    """Frontmatter without an ``id`` field: we fall back to the filename."""
    install_limina(tmp_path, assert_git=False)
    kb = tmp_path / "kb"
    p = kb / "research" / "hypotheses" / "H042-nofm-id.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: hypothesis\n---\n\n# H042 — Inferred\n\nBody.\n",
        encoding="utf-8",
    )
    ref = load_artifact("H042", kb_root=kb)
    assert ref.id == "H042"
    assert ref.title == "Inferred"


# ---------------------------------------------------------------------------
# list_kb_artifacts
# ---------------------------------------------------------------------------


def test_list_kb_artifacts_all(populated_kb: Path) -> None:
    refs = list_kb_artifacts(populated_kb)
    ids = {r.id for r in refs}
    assert {"H001", "E001", "F001"}.issubset(ids)


def test_list_kb_artifacts_by_kind(populated_kb: Path) -> None:
    refs = list_kb_artifacts(populated_kb, kind="E")
    assert [r.id for r in refs] == ["E001"]
    assert all(r.kind == "E" for r in refs)


def test_list_kb_artifacts_skips_malformed(tmp_path: Path) -> None:
    install_limina(tmp_path, assert_git=False)
    kb = tmp_path / "kb"
    # Drop a broken file that can't be parsed or lacks a recoverable id.
    (kb / "research" / "hypotheses" / "H999-broken.md").write_text(
        "not frontmatter, just text", encoding="utf-8"
    )
    # Still succeeds: no id found AND filename has correct prefix so it's valid
    # (recover from filename). Then add a truly unparseable one.
    (kb / "research" / "hypotheses" / "junk.md").write_text(
        "nothing useful", encoding="utf-8"
    )
    refs = list_kb_artifacts(kb, kind="H")
    # H999 is recoverable from filename; junk.md is skipped because no id prefix.
    assert [r.id for r in refs] == ["H999"]
