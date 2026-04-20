"""Tests for ``install_limina``."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_experiments.install import (
    InstallAction,
    block_merge_markdown,
    compute_vendor_sha,
    install_limina,
    is_limina_installed,
    merge_claude_settings,
)
from agentic_experiments.utils.paths import (
    INSTALLED_MARKER_REL,
    read_installed_marker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def fresh_git_repo(tmp_path: Path) -> Path:
    """An empty directory with a ``.git`` dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    return repo


# ---------------------------------------------------------------------------
# compute_vendor_sha
# ---------------------------------------------------------------------------


def test_vendor_sha_is_deterministic() -> None:
    s1 = compute_vendor_sha()
    s2 = compute_vendor_sha()
    assert s1 == s2
    assert len(s1) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
# merge_claude_settings (unit-level)
# ---------------------------------------------------------------------------


def test_merge_claude_settings_into_empty_user() -> None:
    vendor = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "python x.py", "timeout": 5}],
                }
            ]
        }
    }
    merged = merge_claude_settings(vendor, {})
    assert merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "python x.py"


def test_merge_claude_settings_dedupes_identical_hook() -> None:
    entry = {
        "matcher": "",
        "hooks": [{"type": "command", "command": "python x.py", "timeout": 5}],
    }
    existing = {"hooks": {"SessionStart": [entry]}}
    vendor = {"hooks": {"SessionStart": [entry]}}

    merged = merge_claude_settings(vendor, existing)
    assert len(merged["hooks"]["SessionStart"]) == 1


def test_merge_claude_settings_preserves_user_hooks_and_appends_ours() -> None:
    user_hook = {
        "matcher": "Read",
        "hooks": [{"type": "command", "command": "user.sh", "timeout": 5}],
    }
    vendor_hook = {
        "matcher": "Write|Edit",
        "hooks": [{"type": "command", "command": "python guard.py", "timeout": 5}],
    }
    existing = {"hooks": {"PostToolUse": [user_hook]}, "permissions": {"allow": []}}
    vendor = {"hooks": {"PostToolUse": [vendor_hook]}}

    merged = merge_claude_settings(vendor, existing)
    matchers = [group["matcher"] for group in merged["hooks"]["PostToolUse"]]
    assert matchers == ["Read", "Write|Edit"]
    # User's permissions key untouched
    assert merged["permissions"] == {"allow": []}


def test_merge_claude_settings_preserves_user_top_level_keys() -> None:
    existing = {"theme": "dark", "other": {"nested": True}}
    vendor = {"hooks": {"Stop": [{"matcher": "", "hooks": []}]}}
    merged = merge_claude_settings(vendor, existing)
    assert merged["theme"] == "dark"
    assert merged["other"] == {"nested": True}


# ---------------------------------------------------------------------------
# block_merge_markdown (unit-level)
# ---------------------------------------------------------------------------


def test_block_merge_appends_when_markers_absent() -> None:
    existing = "# User doc\n\nuser content\n"
    merged = block_merge_markdown(existing, "vendor content")
    assert "user content" in merged
    assert "<!-- agentic-experiments:begin -->" in merged
    assert "vendor content" in merged
    assert "<!-- agentic-experiments:end -->" in merged


def test_block_merge_replaces_existing_block() -> None:
    existing = (
        "# User doc\n\n"
        "<!-- agentic-experiments:begin -->\nold vendor\n<!-- agentic-experiments:end -->\n"
        "\nmore user content\n"
    )
    merged = block_merge_markdown(existing, "new vendor")
    assert "old vendor" not in merged
    assert "new vendor" in merged
    assert "more user content" in merged


# ---------------------------------------------------------------------------
# install_limina — end-to-end
# ---------------------------------------------------------------------------


def test_install_requires_git_by_default(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not a git repo"):
        install_limina(tmp_path)


def test_install_allows_no_git_when_flag_false(tmp_path: Path) -> None:
    # Should not raise.
    actions = install_limina(tmp_path, assert_git=False)
    assert any(a.kind == "wrote_marker" for a in actions)


def test_install_populates_fresh_repo(fresh_git_repo: Path) -> None:
    actions = install_limina(fresh_git_repo)
    kinds = {a.kind for a in actions}
    assert "copied" in kinds
    assert "initialized_runs" in kinds
    assert "wrote_marker" in kinds

    # Trees landed
    assert (fresh_git_repo / "kb" / "ACTIVE.md").is_file()
    assert (fresh_git_repo / "templates" / "hypothesis.md").is_file()
    assert (fresh_git_repo / "scripts" / "hooks" / "session_start.py").is_file()
    assert (fresh_git_repo / "scripts" / "kb_validate.py").is_file()

    # Claude settings landed at the expected path (merge path copied a new file)
    settings = json.loads((fresh_git_repo / ".claude" / "settings.json").read_text("utf-8"))
    assert "hooks" in settings
    # Hooks commands reference Python ports, not bash
    for event_groups in settings["hooks"].values():
        for group in event_groups:
            for h in group["hooks"]:
                assert "python" in h["command"], h


def test_install_initializes_signac_project(fresh_git_repo: Path) -> None:
    install_limina(fresh_git_repo)
    assert (fresh_git_repo / ".runs").is_dir()
    assert (fresh_git_repo / ".runs" / "signac.rc").is_file() or (
        fresh_git_repo / ".runs" / "workspace"
    ).is_dir() or (
        fresh_git_repo / ".runs" / "signac_project_document.json"
    ).is_file() or any(p.name.startswith("signac") for p in (fresh_git_repo / ".runs").iterdir())


def test_install_writes_valid_marker(fresh_git_repo: Path) -> None:
    install_limina(fresh_git_repo)
    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert marker["version"]
    assert marker["run_store_path"] == ".runs"
    assert len(marker["limina_vendor_sha"]) == 64
    assert is_limina_installed(fresh_git_repo)


def test_install_is_idempotent(fresh_git_repo: Path) -> None:
    install_limina(fresh_git_repo)
    # Second run: every action should be either already_installed (short-circuit)
    # or a no-op kind. Because the marker matches the vendor sha, we short-circuit.
    actions = install_limina(fresh_git_repo)
    assert any(a.kind == "already_installed" for a in actions)
    # Critically: nothing got copied again
    assert not any(a.kind == "copied" for a in actions)


def test_install_force_bypasses_idempotence(fresh_git_repo: Path) -> None:
    install_limina(fresh_git_repo)
    actions = install_limina(fresh_git_repo, force=True)
    # On force, we re-walk the trees; all files should match -> skipped_identical
    # (nothing on disk changed since last install).
    assert not any(a.kind == "already_installed" for a in actions)
    assert any(a.kind == "skipped_identical" for a in actions)


def test_install_refuses_to_clobber_conflicting_file_without_force(
    fresh_git_repo: Path,
) -> None:
    # Pre-create a conflicting user file at the same path we'd copy.
    target = fresh_git_repo / "kb" / "ACTIVE.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-owned content", encoding="utf-8")

    actions = install_limina(fresh_git_repo)
    skipped = [a for a in actions if a.path.endswith("kb/ACTIVE.md")]
    assert skipped
    assert skipped[0].kind == "skipped_conflict"
    # User content preserved
    assert target.read_text(encoding="utf-8") == "user-owned content"


def test_install_force_overwrites_conflicting_file(fresh_git_repo: Path) -> None:
    target = fresh_git_repo / "kb" / "ACTIVE.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-owned content", encoding="utf-8")

    actions = install_limina(fresh_git_repo, force=True)
    overwritten = [a for a in actions if a.path.endswith("kb/ACTIVE.md")]
    assert overwritten
    assert overwritten[0].kind == "copied"
    assert target.read_text(encoding="utf-8") != "user-owned content"


def test_install_json_merges_existing_claude_settings(fresh_git_repo: Path) -> None:
    # Pre-existing user settings: custom matcher + unrelated top-level key.
    claude = fresh_git_repo / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "user-bash.sh", "timeout": 5}
                            ],
                        }
                    ]
                },
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )

    install_limina(fresh_git_repo)
    merged = json.loads((claude / "settings.json").read_text("utf-8"))
    # User's hook survived
    post_tool = merged["hooks"]["PostToolUse"]
    commands = [h["command"] for group in post_tool for h in group["hooks"]]
    assert "user-bash.sh" in commands
    # Vendor's hook got appended
    assert any("kb_write_guard.py" in c for c in commands)
    # User's unrelated key survived
    assert merged["theme"] == "dark"


def test_install_block_merges_existing_agents_md(fresh_git_repo: Path) -> None:
    (fresh_git_repo / "AGENTS.md").write_text(
        "# Existing Agents\n\nuser-defined agent rules\n", encoding="utf-8"
    )
    install_limina(fresh_git_repo)
    content = (fresh_git_repo / "AGENTS.md").read_text("utf-8")
    assert "user-defined agent rules" in content
    assert "<!-- agentic-experiments:begin -->" in content
    assert "<!-- agentic-experiments:end -->" in content


def test_install_creates_claude_dir_if_missing(fresh_git_repo: Path) -> None:
    assert not (fresh_git_repo / ".claude").exists()
    install_limina(fresh_git_repo)
    assert (fresh_git_repo / ".claude" / "settings.json").is_file()


def test_installed_kb_validates_cleanly(fresh_git_repo: Path) -> None:
    """After install, running the vendored kb_validate.py should succeed."""
    install_limina(fresh_git_repo)
    result = subprocess.run(
        [
            "python",
            str(fresh_git_repo / "scripts" / "kb_validate.py"),
            "--kb-root",
            str(fresh_git_repo / "kb"),
            "--format",
            "text",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_install_action_kinds_are_expected(fresh_git_repo: Path) -> None:
    """Every returned InstallAction carries a known kind + a non-empty path."""
    actions = install_limina(fresh_git_repo)
    valid_kinds = {
        "copied",
        "skipped_identical",
        "skipped_conflict",
        "merged_json",
        "merged_block",
        "initialized_runs",
        "wrote_marker",
        "already_installed",
    }
    for a in actions:
        assert isinstance(a, InstallAction)
        assert a.kind in valid_kinds, a
        assert a.path, a
