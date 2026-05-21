"""Tests for sandbox-directory scaffolding."""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from aexp.sandbox import (
    SandboxScaffoldError,
    SandboxScaffoldResult,
    scaffold,
    setup_sandbox_notebook,
    slugify,
)


def _git_init_repo(repo: Path) -> None:
    """Initialize a minimal git repo so find_repo_root can locate it."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh git-initialized repo for sandbox tests.

    Sandbox scaffolding doesn't require the aexp scaffold to be installed
    (it's not a tracked artifact), so we skip the install step
    that test_artifacts.py uses.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git_init_repo(r)
    return r


FIXED_DATE = date(2026, 5, 11)


# ---------------------------------------------------------------------------
# slugify (mirrors test_artifacts.py for consistency)
# ---------------------------------------------------------------------------


def test_slugify_basic() -> None:
    assert slugify("My Experiment Title") == "my-experiment-title"


def test_slugify_strips_punctuation() -> None:
    assert slugify("Foo: bar! (baz)") == "foo-bar-baz"


def test_slugify_empty_falls_back() -> None:
    assert slugify("!!!") == "untitled"


# ---------------------------------------------------------------------------
# scaffold — happy path
# ---------------------------------------------------------------------------


def test_scaffold_creates_directory_tree(repo: Path) -> None:
    result = scaffold(slug="my-exp", repo_root=repo, today=FIXED_DATE)
    assert isinstance(result, SandboxScaffoldResult)
    assert result.slug == "my-exp"
    assert result.dir_name == "2026-05-11_my-exp"
    assert result.dir_path == "notebooks/_sandbox/2026-05-11_my-exp"

    expected = repo / "notebooks" / "_sandbox" / "2026-05-11_my-exp"
    assert expected.is_dir()
    assert (expected / "README.md").is_file()
    assert (expected / "helpers.py").is_file()


def test_scaffold_initializes_root_on_first_call(repo: Path) -> None:
    result = scaffold(slug="first", repo_root=repo, today=FIXED_DATE)
    assert result.root_initialized is True

    sandbox_root = repo / "notebooks" / "_sandbox"
    assert (sandbox_root / "README.md").is_file()
    assert (sandbox_root / ".gitignore").is_file()

    # README files listed in result.files_created
    files = result.files_created
    assert "notebooks/_sandbox/README.md" in files
    assert "notebooks/_sandbox/.gitignore" in files


def test_scaffold_skips_root_init_on_second_call(repo: Path) -> None:
    scaffold(slug="first", repo_root=repo, today=FIXED_DATE)
    result2 = scaffold(slug="second", repo_root=repo, today=FIXED_DATE)
    assert result2.root_initialized is False
    assert "notebooks/_sandbox/README.md" not in result2.files_created
    assert "notebooks/_sandbox/.gitignore" not in result2.files_created


def test_scaffold_preserves_existing_root_readme(repo: Path) -> None:
    """If a user has hand-edited the root README, scaffold shouldn't overwrite it.

    Note: ``root_initialized`` may still be True if the sibling .gitignore was
    newly created — the semantic is "at least one root file was initialized,"
    not "the root was empty." This test specifically guards the
    no-overwrite-of-existing-content behavior.
    """
    sandbox_root = repo / "notebooks" / "_sandbox"
    sandbox_root.mkdir(parents=True)
    custom_readme = "# My custom README\n\nDon't touch this."
    (sandbox_root / "README.md").write_text(custom_readme, encoding="utf-8")

    scaffold(slug="exp", repo_root=repo, today=FIXED_DATE)

    # Custom README content must be preserved verbatim
    assert (sandbox_root / "README.md").read_text(encoding="utf-8") == custom_readme


def test_scaffold_root_initialized_false_when_all_root_files_present(repo: Path) -> None:
    """root_initialized is False only when BOTH root files already existed."""
    sandbox_root = repo / "notebooks" / "_sandbox"
    sandbox_root.mkdir(parents=True)
    (sandbox_root / "README.md").write_text("pre-existing", encoding="utf-8")
    (sandbox_root / ".gitignore").write_text("pre-existing", encoding="utf-8")

    result = scaffold(slug="exp", repo_root=repo, today=FIXED_DATE)
    assert result.root_initialized is False
    assert "notebooks/_sandbox/README.md" not in result.files_created
    assert "notebooks/_sandbox/.gitignore" not in result.files_created


def test_scaffold_renders_title_in_readme(repo: Path) -> None:
    scaffold(
        slug="my-exp",
        title="My Custom Title",
        repo_root=repo,
        today=FIXED_DATE,
    )
    readme = repo / "notebooks" / "_sandbox" / "2026-05-11_my-exp" / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "# 2026-05-11_my-exp — My Custom Title" in text


def test_scaffold_default_title_titlecases_slug(repo: Path) -> None:
    scaffold(slug="my-exp", repo_root=repo, today=FIXED_DATE)
    readme = repo / "notebooks" / "_sandbox" / "2026-05-11_my-exp" / "README.md"
    text = readme.read_text(encoding="utf-8")
    # slug "my-exp" → default title "My Exp"
    assert "— My Exp" in text


def test_scaffold_renders_dir_name_in_helpers(repo: Path) -> None:
    scaffold(slug="my-exp", repo_root=repo, today=FIXED_DATE)
    helpers = repo / "notebooks" / "_sandbox" / "2026-05-11_my-exp" / "helpers.py"
    text = helpers.read_text(encoding="utf-8")
    assert "Sandbox-local helpers for 2026-05-11_my-exp" in text
    # Boilerplate present
    assert "SANDBOX_DIR = Path(__file__).resolve().parent" in text
    assert "REPO_ROOT = find_repo_root(start=SANDBOX_DIR)" in text


# ---------------------------------------------------------------------------
# scaffold — error paths
# ---------------------------------------------------------------------------


def test_scaffold_rejects_duplicate_slug(repo: Path) -> None:
    scaffold(slug="dup", repo_root=repo, today=FIXED_DATE)
    with pytest.raises(SandboxScaffoldError, match="already exists"):
        scaffold(slug="dup", repo_root=repo, today=FIXED_DATE)


def test_scaffold_rejects_empty_slug(repo: Path) -> None:
    with pytest.raises(SandboxScaffoldError, match="slug is required"):
        scaffold(slug="", repo_root=repo)


def test_scaffold_rejects_uppercase_slug(repo: Path) -> None:
    with pytest.raises(SandboxScaffoldError, match="non-slug characters"):
        scaffold(slug="UpperCase", repo_root=repo)


def test_scaffold_rejects_underscore_slug(repo: Path) -> None:
    with pytest.raises(SandboxScaffoldError, match="non-slug characters"):
        scaffold(slug="has_underscore", repo_root=repo)


def test_scaffold_suggests_corrected_slug_in_error(repo: Path) -> None:
    with pytest.raises(SandboxScaffoldError, match="Try 'my-bad-slug'"):
        scaffold(slug="My_Bad_Slug", repo_root=repo)


# ---------------------------------------------------------------------------
# scaffold — parent_dir override
# ---------------------------------------------------------------------------


def test_scaffold_with_relative_parent_dir(repo: Path) -> None:
    """Relative parent_dir resolves under repo_root."""
    result = scaffold(
        slug="exp",
        repo_root=repo,
        parent_dir="custom_sandbox",
        today=FIXED_DATE,
    )
    assert (repo / "custom_sandbox" / "2026-05-11_exp").is_dir()
    assert result.dir_path == "custom_sandbox/2026-05-11_exp"


def test_scaffold_with_absolute_parent_dir_outside_repo(
    repo: Path, tmp_path: Path
) -> None:
    """Absolute parent_dir outside repo: dir_path becomes absolute POSIX."""
    outside = tmp_path / "outside_repo_sandbox"
    result = scaffold(
        slug="exp",
        repo_root=repo,
        parent_dir=outside,
        today=FIXED_DATE,
    )
    assert (outside / "2026-05-11_exp").is_dir()
    # dir_path falls back to absolute since the dir isn't under repo
    assert result.dir_path.endswith("outside_repo_sandbox/2026-05-11_exp")


# ---------------------------------------------------------------------------
# setup_sandbox_notebook
# ---------------------------------------------------------------------------


def test_setup_sandbox_notebook_raises_if_missing(repo: Path) -> None:
    with pytest.raises(FileNotFoundError, match="sandbox subdir not found"):
        setup_sandbox_notebook("2026-05-11_does-not-exist", start=repo)


def test_setup_sandbox_notebook_inserts_on_sys_path(repo: Path) -> None:
    scaffold(slug="boot-test", repo_root=repo, today=FIXED_DATE)

    sandbox_dir = repo / "notebooks" / "_sandbox" / "2026-05-11_boot-test"
    sandbox_str = str(sandbox_dir)

    # Clean up sys.path before + after
    if sandbox_str in sys.path:
        sys.path.remove(sandbox_str)

    try:
        ctx = setup_sandbox_notebook("2026-05-11_boot-test", start=repo)
        assert ctx["sandbox_dir"] == sandbox_dir
        assert ctx["repo_root"] == repo.resolve()
        assert sys.path[0] == sandbox_str
    finally:
        if sandbox_str in sys.path:
            sys.path.remove(sandbox_str)


def test_setup_sandbox_notebook_idempotent_on_sys_path(repo: Path) -> None:
    """Calling twice doesn't duplicate the sys.path entry."""
    scaffold(slug="idem", repo_root=repo, today=FIXED_DATE)
    sandbox_dir = repo / "notebooks" / "_sandbox" / "2026-05-11_idem"
    sandbox_str = str(sandbox_dir)

    if sandbox_str in sys.path:
        sys.path.remove(sandbox_str)

    try:
        setup_sandbox_notebook("2026-05-11_idem", start=repo)
        setup_sandbox_notebook("2026-05-11_idem", start=repo)
        assert sys.path.count(sandbox_str) == 1
    finally:
        if sandbox_str in sys.path:
            sys.path.remove(sandbox_str)
