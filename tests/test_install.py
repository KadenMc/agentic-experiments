"""Tests for ``install_scaffold``."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aexp.install import (
    InstallAction,
    InstallRefused,
    block_merge_markdown,
    compute_scaffold_sha,
    install_scaffold,
    is_scaffold_installed,
    merge_claude_settings,
)
from aexp.utils.paths import (
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
# compute_scaffold_sha
# ---------------------------------------------------------------------------


def test_scaffold_sha_is_deterministic() -> None:
    s1 = compute_scaffold_sha()
    s2 = compute_scaffold_sha()
    assert s1 == s2
    assert len(s1) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
# merge_claude_settings (unit-level)
# ---------------------------------------------------------------------------


def test_merge_claude_settings_into_empty_user() -> None:
    shipped = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "python x.py", "timeout": 5}],
                }
            ]
        }
    }
    merged = merge_claude_settings(shipped, {})
    assert merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "python x.py"


def test_merge_claude_settings_dedupes_identical_hook() -> None:
    entry = {
        "matcher": "",
        "hooks": [{"type": "command", "command": "python x.py", "timeout": 5}],
    }
    existing = {"hooks": {"SessionStart": [entry]}}
    shipped = {"hooks": {"SessionStart": [entry]}}

    merged = merge_claude_settings(shipped, existing)
    assert len(merged["hooks"]["SessionStart"]) == 1


def test_merge_claude_settings_preserves_user_hooks_and_appends_ours() -> None:
    user_hook = {
        "matcher": "Read",
        "hooks": [{"type": "command", "command": "user.sh", "timeout": 5}],
    }
    shipped_hook = {
        "matcher": "Write|Edit",
        "hooks": [{"type": "command", "command": "python guard.py", "timeout": 5}],
    }
    existing = {"hooks": {"PostToolUse": [user_hook]}, "permissions": {"allow": []}}
    shipped = {"hooks": {"PostToolUse": [shipped_hook]}}

    merged = merge_claude_settings(shipped, existing)
    matchers = [group["matcher"] for group in merged["hooks"]["PostToolUse"]]
    assert matchers == ["Read", "Write|Edit"]
    # User's permissions key untouched
    assert merged["permissions"] == {"allow": []}


def test_merge_claude_settings_preserves_user_top_level_keys() -> None:
    existing = {"theme": "dark", "other": {"nested": True}}
    shipped = {"hooks": {"Stop": [{"matcher": "", "hooks": []}]}}
    merged = merge_claude_settings(shipped, existing)
    assert merged["theme"] == "dark"
    assert merged["other"] == {"nested": True}


# ---------------------------------------------------------------------------
# block_merge_markdown (unit-level)
# ---------------------------------------------------------------------------


def test_block_merge_appends_when_markers_absent() -> None:
    existing = "# User doc\n\nuser content\n"
    merged = block_merge_markdown(existing, "shipped content")
    assert "user content" in merged
    assert "<!-- agentic-experiments:begin -->" in merged
    assert "shipped content" in merged
    assert "<!-- agentic-experiments:end -->" in merged


def test_block_merge_replaces_existing_block() -> None:
    existing = (
        "# User doc\n\n"
        "<!-- agentic-experiments:begin -->\nold block\n<!-- agentic-experiments:end -->\n"
        "\nmore user content\n"
    )
    merged = block_merge_markdown(existing, "new block")
    assert "old block" not in merged
    assert "new block" in merged
    assert "more user content" in merged


# ---------------------------------------------------------------------------
# install_scaffold — end-to-end
# ---------------------------------------------------------------------------


def test_install_requires_git_by_default(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not a git repo"):
        install_scaffold(tmp_path)


def test_install_allows_no_git_when_flag_false(tmp_path: Path) -> None:
    # Should not raise.
    actions = install_scaffold(tmp_path, assert_git=False)
    assert any(a.kind == "wrote_marker" for a in actions)


def test_install_populates_fresh_repo(fresh_git_repo: Path) -> None:
    actions = install_scaffold(fresh_git_repo)
    kinds = {a.kind for a in actions}
    assert "copied" in kinds
    assert "initialized_runs" in kinds
    assert "wrote_marker" in kinds

    # Trees landed: kb/ data and templates, but NOT scripts/. Hook scripts
    # now live inside the installed aexp package and are invoked from there.
    assert (fresh_git_repo / "kb" / "ACTIVE.md").is_file()
    assert (fresh_git_repo / "templates" / "hypothesis.md").is_file()
    assert not (fresh_git_repo / "scripts").exists()

    # Claude settings landed at the expected path (merge path copied a new file)
    settings = json.loads((fresh_git_repo / ".claude" / "settings.json").read_text("utf-8"))
    assert "hooks" in settings
    # Hook commands invoke the installed aexp package — `{python_exe} -m
    # aexp.hooks.<name>` — not a copied script path.
    for event_groups in settings["hooks"].values():
        for group in event_groups:
            for h in group["hooks"]:
                assert "-m aexp.hooks." in h["command"], h


def test_install_drops_slash_commands_without_a_second_step(
    fresh_git_repo: Path,
) -> None:
    """``aexp install`` must ship the slash commands into ``.claude/commands/``.

    Previously users had to remember to run ``aexp install-slash-commands`` as
    a second step; this test pins the folded-in behaviour so the standalone
    verb can't become load-bearing again.
    """
    install_scaffold(fresh_git_repo)
    commands = fresh_git_repo / ".claude" / "commands"
    assert commands.is_dir()
    for name in (
        "aexp-new-run.md",
        "aexp-finding-from-run.md",
        "aexp-finding-from-batch.md",
        "aexp-finding-placeholder.md",
        "aexp-queue-add.md",
        "aexp-queue-materialize.md",
        "aexp-new-thread.md",
        "aexp-close-thread.md",
    ):
        assert (commands / name).is_file(), name


def test_install_initializes_signac_project(fresh_git_repo: Path) -> None:
    install_scaffold(fresh_git_repo)
    assert (fresh_git_repo / ".runs").is_dir()
    assert (fresh_git_repo / ".runs" / "signac.rc").is_file() or (
        fresh_git_repo / ".runs" / "workspace"
    ).is_dir() or (
        fresh_git_repo / ".runs" / "signac_project_document.json"
    ).is_file() or any(p.name.startswith("signac") for p in (fresh_git_repo / ".runs").iterdir())


def test_install_writes_valid_marker(fresh_git_repo: Path) -> None:
    install_scaffold(fresh_git_repo)
    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert marker["version"]
    assert marker["run_store_path"] == ".runs"
    assert len(marker["scaffold_sha"]) == 64
    # Cross-platform invocation fields written by default.
    assert "python_exe" in marker
    assert Path(marker["python_exe"]).exists()
    # conda_env_name is present (may be "" when running under a venv).
    assert "conda_env_name" in marker
    assert is_scaffold_installed(fresh_git_repo)


def test_install_is_idempotent(fresh_git_repo: Path) -> None:
    install_scaffold(fresh_git_repo)
    # Second run: every action should be either already_installed (short-circuit)
    # or a no-op kind. Because the marker matches the scaffold sha, we short-circuit.
    actions = install_scaffold(fresh_git_repo)
    assert any(a.kind == "already_installed" for a in actions)
    # Critically: nothing got copied again
    assert not any(a.kind == "copied" for a in actions)


def test_install_force_bypasses_idempotence(fresh_git_repo: Path) -> None:
    install_scaffold(fresh_git_repo)
    actions = install_scaffold(fresh_git_repo, force=True)
    # On force, we re-walk the trees; all files should match -> skipped_identical
    # (nothing on disk changed since last install).
    assert not any(a.kind == "already_installed" for a in actions)
    assert any(a.kind == "skipped_identical" for a in actions)


def test_install_preserves_user_modified_kb_content_without_force(
    fresh_git_repo: Path,
) -> None:
    """User-authored content in the ``kb/`` scaffold is preserved without
    ``--force`` via the ``preserved_user_modified`` action — no clobber,
    and no "rerun with --force" advice (which was always dangerous for
    this scope)."""
    target = fresh_git_repo / "kb" / "ACTIVE.md"
    target.parent.mkdir(parents=True)
    target.write_text("user-owned content", encoding="utf-8")

    actions = install_scaffold(fresh_git_repo)
    entries = [a for a in actions if a.path.endswith("kb/ACTIVE.md")]
    assert entries
    assert entries[0].kind == "preserved_user_modified"
    # User content untouched.
    assert target.read_text(encoding="utf-8") == "user-owned content"


def test_install_preserves_user_modified_kb_content_even_under_force(
    fresh_git_repo: Path,
) -> None:
    """Under ``--force``, user-authored ``kb/`` scaffold files still survive.

    This is the regression guard for the 2026-04-24 consumer-side CHALLENGE.md
    clobber: `aexp install --yes --force --dev` had overwritten a committed
    mission file back to the blank stub. `--force` should refresh pinned
    tooling (slash commands, skills, hooks) — not destroy user-authored
    scaffold content.
    """
    target = fresh_git_repo / "kb" / "mission" / "CHALLENGE.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# My research mission\n\nSpecific objective X.\n", encoding="utf-8"
    )

    actions = install_scaffold(fresh_git_repo, force=True)
    entries = [a for a in actions if a.path.endswith("kb/mission/CHALLENGE.md")]
    assert entries
    assert entries[0].kind == "preserved_user_modified"
    # Content survived the force re-install.
    assert "My research mission" in target.read_text(encoding="utf-8")


def test_install_preserves_user_modified_templates(fresh_git_repo: Path) -> None:
    """Templates (``templates/*.md``) are user-customisable — aexp's
    ``artifacts._load_template`` prefers the repo-local copy if present.
    So they ride in the same preservation scope as ``kb/``."""
    target = fresh_git_repo / "templates" / "hypothesis.md"
    target.parent.mkdir(parents=True)
    target.write_text("# My custom hypothesis template\n", encoding="utf-8")

    actions = install_scaffold(fresh_git_repo, force=True)
    entries = [
        a for a in actions if a.path.endswith("templates/hypothesis.md")
    ]
    assert entries
    assert entries[0].kind == "preserved_user_modified"
    assert "My custom hypothesis template" in target.read_text(encoding="utf-8")


def test_install_refreshes_default_kb_content_under_force(
    fresh_git_repo: Path,
) -> None:
    """If a kb/ file still byte-matches the shipped default, a re-install
    should report ``skipped_identical`` — no preservation noise, and no
    copy since the content is already correct."""
    install_scaffold(fresh_git_repo)  # first run lays down the defaults
    actions = install_scaffold(fresh_git_repo, force=True)
    # Path filter: files shipped under kb/ always end with "kb/<...>.md".
    kb_entries = [
        a
        for a in actions
        if "/kb/" in a.path and a.path.endswith(".md")
    ]
    assert kb_entries, [(a.kind, a.path) for a in actions]
    # None were preserved (all match shipped default); all skipped_identical.
    assert all(e.kind == "skipped_identical" for e in kb_entries), [
        (e.kind, e.path) for e in kb_entries
    ]


def test_install_force_still_overwrites_stale_slash_commands(
    fresh_git_repo: Path,
) -> None:
    """Slash commands are tooling, not user content. If a user had a stale
    copy of a shipped command on disk, ``--force`` SHOULD refresh it to
    match the current shipped version. The preservation path does not
    apply to ``.claude/commands/``."""
    target = fresh_git_repo / ".claude" / "commands" / "aexp-new-run.md"
    target.parent.mkdir(parents=True)
    target.write_text("# stale hand-edited content\n", encoding="utf-8")

    actions = install_scaffold(fresh_git_repo, force=True)
    entries = [
        a
        for a in actions
        if a.path.endswith(".claude/commands/aexp-new-run.md")
    ]
    assert entries
    assert entries[0].kind == "copied"
    assert "stale hand-edited content" not in target.read_text(encoding="utf-8")


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

    install_scaffold(fresh_git_repo)
    merged = json.loads((claude / "settings.json").read_text("utf-8"))
    # User's hook survived
    post_tool = merged["hooks"]["PostToolUse"]
    commands = [h["command"] for group in post_tool for h in group["hooks"]]
    assert "user-bash.sh" in commands
    # The shipped hook got appended
    assert any("aexp.hooks.kb_write_guard" in c for c in commands)
    # User's unrelated key survived
    assert merged["theme"] == "dark"


def test_install_block_merges_existing_agents_md(fresh_git_repo: Path) -> None:
    (fresh_git_repo / "AGENTS.md").write_text(
        "# Existing Agents\n\nuser-defined agent rules\n", encoding="utf-8"
    )
    install_scaffold(fresh_git_repo)
    content = (fresh_git_repo / "AGENTS.md").read_text("utf-8")
    assert "user-defined agent rules" in content
    assert "<!-- agentic-experiments:begin -->" in content
    assert "<!-- agentic-experiments:end -->" in content


def test_install_creates_claude_dir_if_missing(fresh_git_repo: Path) -> None:
    assert not (fresh_git_repo / ".claude").exists()
    install_scaffold(fresh_git_repo)
    assert (fresh_git_repo / ".claude" / "settings.json").is_file()


def test_installed_kb_validates_cleanly(fresh_git_repo: Path) -> None:
    """After install, the seeded kb/ passes structural validation."""
    from aexp.kb_validate import validate_kb

    install_scaffold(fresh_git_repo)
    result = validate_kb(fresh_git_repo / "kb")
    assert result.ok, result.errors


def test_install_action_kinds_are_expected(fresh_git_repo: Path) -> None:
    """Every returned InstallAction carries a known kind + a non-empty path."""
    actions = install_scaffold(fresh_git_repo)
    valid_kinds = {
        "copied",
        "skipped_identical",
        "skipped_conflict",
        "preserved_user_modified",
        "merged_json",
        "merged_block",
        "merged_gitignore",
        "gitignore_migration_warning",
        "initialized_runs",
        "installed_skill",
        "wrote_marker",
        "already_installed",
    }
    for a in actions:
        assert isinstance(a, InstallAction)
        assert a.kind in valid_kinds, a
        assert a.path, a


def test_install_copies_skills_to_claude_skills(fresh_git_repo: Path) -> None:
    """All bundled research skills must land under <repo>/.claude/skills/.

    Without these, the AGENTS.md references like $experiment-rigor are broken
    on every consumer repo.
    """
    install_scaffold(fresh_git_repo)
    skills_root = fresh_git_repo / ".claude" / "skills"
    assert skills_root.is_dir()
    # Research-methodology skills (from scaffold/skills/)
    expected = {
        "experiment-rigor",
        "exploratory-sota-research",
        "research-devil-advocate",
        "build-maintainable-software",
    }
    installed = {p.name for p in skills_root.iterdir() if p.is_dir()}
    assert expected.issubset(installed), (expected, installed)
    for name in expected:
        assert (skills_root / name / "SKILL.md").is_file(), name


def test_install_skills_emits_installed_skill_actions(fresh_git_repo: Path) -> None:
    actions = install_scaffold(fresh_git_repo)
    skill_actions = [a for a in actions if a.kind == "installed_skill"]
    # 4 research-methodology skills = 4 installed_skill entries.
    assert len(skill_actions) == 4, [a.path for a in skill_actions]
    paths = {Path(a.path).name for a in skill_actions}
    assert "experiment-rigor" in paths
    assert "experiment-rigor" in paths


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


def test_install_writes_mcp_json_at_repo_root(fresh_git_repo: Path) -> None:
    """``.mcp.json`` at the repo root must contain an ``mcpServers.aexp`` entry.

    This is the file Claude Code reads for project-scope MCP servers;
    ``.claude/settings.json`` does NOT drive MCP config.
    """
    install_scaffold(fresh_git_repo)
    mcp_path = fresh_git_repo / ".mcp.json"
    assert mcp_path.is_file(), "install must write .mcp.json at repo root"
    mcp = json.loads(mcp_path.read_text("utf-8"))
    assert "mcpServers" in mcp
    assert "aexp" in mcp["mcpServers"]
    entry = mcp["mcpServers"]["aexp"]
    assert entry["command"]
    # The command reaches the aexp-mcp-server entry point (added to
    # [project.scripts] in pyproject.toml) via uvx.
    combined = [entry["command"]] + list(entry["args"])
    assert "aexp-mcp-server" in combined, combined


def test_install_does_not_write_mcp_servers_to_settings_json(
    fresh_git_repo: Path,
) -> None:
    """``mcpServers`` must NOT end up in ``.claude/settings.json`` — Claude
    Code would ignore it there. All MCP config belongs in ``.mcp.json``.
    """
    install_scaffold(fresh_git_repo)
    settings = json.loads(
        (fresh_git_repo / ".claude" / "settings.json").read_text("utf-8")
    )
    assert "mcpServers" not in settings, (
        "mcpServers leaked into settings.json; it must live in .mcp.json"
    )


def test_install_mcp_entry_uses_uvx(fresh_git_repo: Path) -> None:
    """MCP command must use ``uvx`` — portable, no absolute paths.

    This is the canonical pattern for Python MCP servers (see
    modelcontextprotocol/servers reference implementations + Anthropic's
    MCP quickstart). It lets ``.mcp.json`` be committed to git because
    every teammate with ``uv`` installed gets the same invocation.
    """
    install_scaffold(fresh_git_repo)
    mcp = json.loads((fresh_git_repo / ".mcp.json").read_text("utf-8"))
    entry = mcp["mcpServers"]["aexp"]
    assert entry["command"] == "uvx"
    # --from <spec> aexp-mcp-server. The spec must name the PyPI
    # distribution with the [mcp] extra so uvx installs the server's deps.
    assert "--from" in entry["args"]
    from_idx = entry["args"].index("--from")
    spec = entry["args"][from_idx + 1]
    assert spec.startswith("agentic-experiments")
    assert "mcp" in spec  # [mcp] extra
    # The entry-point script name (matches pyproject [project.scripts]).
    assert "aexp-mcp-server" in entry["args"]
    # PYTHONUNBUFFERED=1 still set as belt-and-suspenders for stdio.
    assert entry["env"].get("PYTHONUNBUFFERED") == "1"


def test_install_dev_mcp_entry_uses_current_interpreter(fresh_git_repo: Path) -> None:
    """``install_scaffold(..., dev=True)`` writes a direct-Python MCP invocation
    so editable installs (``pip install -e``) take effect on the MCP surface.
    """
    import sys

    install_scaffold(fresh_git_repo, dev=True)
    mcp = json.loads((fresh_git_repo / ".mcp.json").read_text("utf-8"))
    entry = mcp["mcpServers"]["aexp"]
    # Command is the running interpreter, not uvx.
    assert entry["command"] == sys.executable
    assert entry["command"] != "uvx"
    # Args invoke the MCP server as a module; no --from / PyPI spec.
    assert entry["args"] == ["-m", "aexp.mcp_server"]
    assert "--from" not in entry["args"]
    # Stdio flushing guard preserved.
    assert entry["env"].get("PYTHONUNBUFFERED") == "1"


def test_install_dev_flag_can_be_toggled_on_reinstall(fresh_git_repo: Path) -> None:
    """Running install without --dev after a dev install rewrites back to uvx."""
    install_scaffold(fresh_git_repo, dev=True)
    mcp_dev = json.loads((fresh_git_repo / ".mcp.json").read_text("utf-8"))
    assert mcp_dev["mcpServers"]["aexp"]["command"] != "uvx"

    install_scaffold(fresh_git_repo, force=True, dev=False)
    mcp_prod = json.loads((fresh_git_repo / ".mcp.json").read_text("utf-8"))
    assert mcp_prod["mcpServers"]["aexp"]["command"] == "uvx"


def test_install_mcp_entry_is_env_independent(
    fresh_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uvx invocation must be identical regardless of which env the
    installer was run from — no conda-env name, no absolute python path,
    no user home directory leaked into .mcp.json.
    """
    import sys

    # Set a distinctive conda env to confirm we DON'T leak it.
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "some-distinctive-env-name")
    install_scaffold(fresh_git_repo)
    content = (fresh_git_repo / ".mcp.json").read_text("utf-8")
    # No env-specific strings should appear anywhere in the written file.
    assert "some-distinctive-env-name" not in content
    assert sys.executable not in content
    assert "miniforge3" not in content
    assert "miniconda" not in content
    assert "conda" not in content  # neither "conda run" nor env prefix paths
    # And no Users-style absolute home directory.
    assert "Users\\" not in content
    assert "/home/" not in content


def test_install_preserves_user_mcp_servers_in_mcp_json(
    fresh_git_repo: Path,
) -> None:
    """User-defined servers in ``.mcp.json`` must survive the merge."""
    user_mcp = fresh_git_repo / ".mcp.json"
    user_mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "user-mcp": {
                        "command": "node",
                        "args": ["my-server.js"],
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    install_scaffold(fresh_git_repo)
    merged = json.loads(user_mcp.read_text("utf-8"))
    assert "user-mcp" in merged["mcpServers"]
    assert "aexp" in merged["mcpServers"]


def test_install_overwrites_stale_aexp_mcp_entry_on_reinstall(
    fresh_git_repo: Path,
) -> None:
    """A stale ``aexp`` entry in ``.mcp.json`` gets refreshed on install."""
    user_mcp = fresh_git_repo / ".mcp.json"
    user_mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aexp": {
                        "command": "python",
                        "args": ["-m", "stale_module"],
                        "env": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    install_scaffold(fresh_git_repo)
    mcp = json.loads(user_mcp.read_text("utf-8"))
    combined = (
        [mcp["mcpServers"]["aexp"]["command"]]
        + mcp["mcpServers"]["aexp"]["args"]
    )
    assert "aexp-mcp-server" in combined
    assert "stale_module" not in combined


# ---------------------------------------------------------------------------
# Source-tree self-install guard (added 0.2.1)
# ---------------------------------------------------------------------------
#
# Motivating failure: invoking ``poetry -C <aexp-repo> run aexp install``
# from a separate scratch directory. Poetry's ``-C`` swaps the subprocess
# cwd to the project, so the install ended up materializing the consumer
# scaffold inside the dev repo instead of the user's intended target. The
# guard refuses this class of mistake before any filesystem writes.


def _plant_aexp_source_tree(path: Path) -> None:
    """Plant a fake ``pyproject.toml`` that names the dir as the aexp source tree.

    Used by the self-install-guard tests — the guard's detection rule is
    text-based on pyproject contents, so any directory with a matching
    pyproject is treated as the source tree regardless of what else lives
    there. Tests use this to simulate the dev repo without copying its
    full contents.
    """
    (path / "pyproject.toml").write_text(
        '[project]\nname = "agentic-experiments"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )


def test_install_refuses_aexp_source_tree(fresh_git_repo: Path) -> None:
    """``install_scaffold`` refuses when cwd is the aexp source tree itself."""
    _plant_aexp_source_tree(fresh_git_repo)
    with pytest.raises(InstallRefused) as exc_info:
        install_scaffold(fresh_git_repo)
    msg = str(exc_info.value)
    assert "agentic-experiments source tree" in msg
    assert "--allow-self-install" in msg
    # No filesystem pollution: the guard runs before any writes.
    assert not (fresh_git_repo / "kb").exists()
    assert not (fresh_git_repo / ".aexp").exists()
    assert not (fresh_git_repo / ".claude").exists()


def test_install_refuses_subdirectory_of_source_tree(
    fresh_git_repo: Path,
) -> None:
    """The guard's walk-up matches when invoked from a subdir of the source tree.

    Real-world equivalent: invoking ``aexp install`` from inside ``src/``
    or ``tests/`` of the dev repo by mistake. The pyproject.toml lives at
    the parent; the walk-up has to find it.
    """
    _plant_aexp_source_tree(fresh_git_repo)
    subdir = fresh_git_repo / "src"
    subdir.mkdir()
    # `assert_git=False` because the subdir doesn't have its own .git.
    with pytest.raises(InstallRefused):
        install_scaffold(subdir, assert_git=False)


def test_install_allow_self_install_overrides_guard(
    fresh_git_repo: Path,
) -> None:
    """``allow_self_install=True`` lets the install proceed inside the source tree.

    Used when dogfooding the consumer scaffold against the dev repo on
    purpose. Should be rare; the flag exists so the guard is policy, not
    an inescapable hard block.
    """
    _plant_aexp_source_tree(fresh_git_repo)
    actions = install_scaffold(fresh_git_repo, allow_self_install=True)
    # Got past the guard and produced a normal install action list.
    assert any(a.kind == "wrote_marker" for a in actions)
    assert (fresh_git_repo / ".aexp" / "installed.json").is_file()


def test_install_allows_consumer_repo_with_different_name(
    fresh_git_repo: Path,
) -> None:
    """Consumer repos (different package name) are unaffected by the guard."""
    (fresh_git_repo / "pyproject.toml").write_text(
        '[project]\nname = "my-research"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    actions = install_scaffold(fresh_git_repo)
    assert any(a.kind == "wrote_marker" for a in actions)
    assert (fresh_git_repo / ".aexp" / "installed.json").is_file()


def test_install_allows_repo_without_pyproject(fresh_git_repo: Path) -> None:
    """Repos with no pyproject anywhere in the walk-up are unaffected.

    The typical "consumer is just a research repo, not a Python package"
    case. The guard's walk-up walks all the way to the filesystem root
    without finding a matching pyproject and falls through to the normal
    install path.
    """
    # No pyproject.toml planted.
    actions = install_scaffold(fresh_git_repo)
    assert any(a.kind == "wrote_marker" for a in actions)
    assert (fresh_git_repo / ".aexp" / "installed.json").is_file()


def test_install_dry_run_also_refuses_source_tree(fresh_git_repo: Path) -> None:
    """The guard fires under ``dry_run`` too, so previewing doesn't leak files.

    Important because the install summary printed under dry-run could
    otherwise mislead a user into thinking the install would succeed.
    """
    _plant_aexp_source_tree(fresh_git_repo)
    with pytest.raises(InstallRefused):
        install_scaffold(fresh_git_repo, dry_run=True)
    # Confirm dry_run path didn't leak anything before the raise.
    assert not (fresh_git_repo / "kb").exists()
    assert not (fresh_git_repo / ".aexp").exists()


# ---------------------------------------------------------------------------
# --with-jupyter install branch
# ---------------------------------------------------------------------------


def test_install_with_jupyter_writes_mcp_entries(fresh_git_repo: Path) -> None:
    """--with-jupyter writes the jupyter entry to .mcp.json alongside the
    existing aexp entry. The legacy jupyter-compute server is not emitted."""
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True)
    mcp_json = json.loads((fresh_git_repo / ".mcp.json").read_text(encoding="utf-8"))
    servers = mcp_json["mcpServers"]
    assert "aexp" in servers
    assert "jupyter" in servers
    assert servers["jupyter"]["command"] == "uvx"
    # jupyter-mcp-server is pinned: v1.0.x's mandatory startup-env auth
    # hangs the MCP stdio handshake (see _jupyter_mcp_entries docstring).
    assert servers["jupyter"]["args"] == ["jupyter-mcp-server==0.23.0"]
    assert "jupyter-compute" not in servers


def test_install_without_jupyter_omits_mcp_entries(fresh_git_repo: Path) -> None:
    """Default install (no --with-jupyter) does NOT write the jupyter entry."""
    install_scaffold(fresh_git_repo, dev=True)
    mcp_json = json.loads((fresh_git_repo / ".mcp.json").read_text(encoding="utf-8"))
    servers = mcp_json["mcpServers"]
    assert "aexp" in servers
    assert "jupyter" not in servers
    assert "jupyter-compute" not in servers


def test_install_with_jupyter_records_marker(fresh_git_repo: Path) -> None:
    """Marker records jupyter_enabled=True after --with-jupyter install."""
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True)
    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert marker.get("jupyter_enabled") is True


def test_install_without_jupyter_marker_omits_field(fresh_git_repo: Path) -> None:
    """Default install does not add jupyter_enabled to the marker (sticky-true semantics)."""
    install_scaffold(fresh_git_repo, dev=True)
    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert "jupyter_enabled" not in marker


def test_install_jupyter_marker_is_sticky_true(fresh_git_repo: Path) -> None:
    """Once --with-jupyter is set, a later install without the flag preserves jupyter_enabled=True."""
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True)
    # Re-install with force to bypass the already-installed short-circuit.
    install_scaffold(fresh_git_repo, dev=True, force=True)
    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert marker.get("jupyter_enabled") is True


def test_install_with_jupyter_copies_setup_doc(fresh_git_repo: Path) -> None:
    """docs/setup/jupyter-mcp.md is copied verbatim from the scaffold when --with-jupyter."""
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True)
    setup_doc = fresh_git_repo / "docs" / "setup" / "jupyter-mcp.md"
    assert setup_doc.is_file()
    body = setup_doc.read_text(encoding="utf-8")
    # A few load-bearing strings from the bundled doc.
    assert "Adapting this guide to your cluster" in body
    assert "Investigation log" in body


def test_install_with_jupyter_idempotent(fresh_git_repo: Path) -> None:
    """Running install twice with the same flags doesn't change .mcp.json."""
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True)
    first = (fresh_git_repo / ".mcp.json").read_text(encoding="utf-8")
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True, force=True)
    second = (fresh_git_repo / ".mcp.json").read_text(encoding="utf-8")
    assert json.loads(first) == json.loads(second)


def test_install_with_jupyter_preserves_user_entries(fresh_git_repo: Path) -> None:
    """User-defined .mcp.json entries survive a --with-jupyter install."""
    custom = {
        "mcpServers": {
            "my_custom": {"command": "echo", "args": ["hello"]},
        }
    }
    (fresh_git_repo / ".mcp.json").write_text(json.dumps(custom), encoding="utf-8")
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True)
    mcp_json = json.loads((fresh_git_repo / ".mcp.json").read_text(encoding="utf-8"))
    servers = mcp_json["mcpServers"]
    assert "my_custom" in servers
    assert servers["my_custom"]["command"] == "echo"
    assert "aexp" in servers
    assert "jupyter" in servers


def test_install_with_jupyter_preserves_existing_jupyter_entry(fresh_git_repo: Path) -> None:
    """If the user has customized their `jupyter` entry, re-running
    --with-jupyter must not clobber it: the install only ever ADDS the
    entry, never overwrites an existing one.
    """
    custom = {
        "mcpServers": {
            "jupyter": {
                "command": "uvx",
                "args": ["jupyter-mcp-server"],
                "env": {"MY_CUSTOM_MARKER": "kept"},
            }
        }
    }
    (fresh_git_repo / ".mcp.json").write_text(json.dumps(custom), encoding="utf-8")
    install_scaffold(fresh_git_repo, with_jupyter=True, dev=True)
    servers = json.loads(
        (fresh_git_repo / ".mcp.json").read_text(encoding="utf-8")
    )["mcpServers"]
    # The user's customized jupyter entry survives untouched — our generated
    # entry has no `env` block, so its presence proves we did not overwrite.
    assert servers["jupyter"].get("env") == {"MY_CUSTOM_MARKER": "kept"}
    # And the aexp entry was still written.
    assert "aexp" in servers


def test_install_with_jupyter_slash_command_always_present(fresh_git_repo: Path) -> None:
    """The /aexp-jupyter-iterate slash command is installed regardless of --with-jupyter
    (it self-checks tool availability at runtime)."""
    install_scaffold(fresh_git_repo, dev=True)  # NOTE: no --with-jupyter
    slash_cmd = fresh_git_repo / ".claude" / "commands" / "aexp-jupyter-iterate.md"
    assert slash_cmd.is_file()


def test_install_writes_promote_nb_slash_command(fresh_git_repo: Path) -> None:
    """The /aexp-promote-nb slash command is installed during default install,
    and its body contains the load-bearing guardrails (refuses without an
    experiment ID, references the jupyter MCP family, refuses to
    invent a tracked_notebook_run API)."""
    install_scaffold(fresh_git_repo, dev=True)
    slash_cmd = fresh_git_repo / ".claude" / "commands" / "aexp-promote-nb.md"
    assert slash_cmd.is_file()
    body = slash_cmd.read_text(encoding="utf-8")
    # Frontmatter present and well-formed.
    assert body.startswith("---\n")
    assert "description:" in body.split("---", 2)[1]
    # Self-check guidance for tool availability — degrades gracefully without MCP.
    assert "mcp__jupyter__" in body
    # The experiment-required guardrail (without it, promotion lands code in
    # experiments/ that has no H/E chain — the failure mode this command
    # exists to prevent).
    assert "Refuse to proceed without a real experiment" in body
    # Don't fabricate a tracked_notebook_run API — this is the explicit
    # design rejection from the plan discussion.
    assert "tracked_notebook_run" in body  # mentioned only in the "do not invent" warning
    assert "isn't" in body or "is not" in body  # ... in the "there isn't one" disclaimer


# ---------------------------------------------------------------------------
# Line-endings normalization (CRLF / LF cross-platform)
# ---------------------------------------------------------------------------


def test_files_identical_treats_crlf_and_lf_as_equal_for_text(tmp_path: Path) -> None:
    """A CRLF source vs LF target (or vice versa) compares equal for text files.

    This is the load-bearing fix for the cross-platform install bug: a wheel
    built on Windows with CRLF, copied to a consumer with LF on disk, was
    forever reporting `skipped_conflict` on re-install.
    """
    from aexp.install import _files_identical

    crlf = tmp_path / "crlf.md"
    lf = tmp_path / "lf.md"
    crlf.write_bytes(b"---\r\ndescription: x\r\n---\r\nbody\r\n")
    lf.write_bytes(b"---\ndescription: x\n---\nbody\n")
    assert _files_identical(crlf, lf)
    assert _files_identical(lf, crlf)


def test_files_identical_still_distinguishes_real_content_differences(tmp_path: Path) -> None:
    """EOL-normalized comparison must not collapse genuine content differences."""
    from aexp.install import _files_identical

    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_bytes(b"hello\n")
    b.write_bytes(b"hello world\n")
    assert not _files_identical(a, b)


def test_files_identical_does_not_normalize_binary_extensions(tmp_path: Path) -> None:
    """A spurious 0x0d byte in a binary file MUST keep it distinguishable."""
    from aexp.install import _files_identical

    a = tmp_path / "icon.png"
    b = tmp_path / "icon2.png"
    a.write_bytes(b"\x89PNG\r\nfoo")
    b.write_bytes(b"\x89PNG\nfoo")
    # These two byte sequences differ; for a binary file we must not claim
    # equality just because EOL-normalization happens to make them match.
    assert not _files_identical(a, b)


def test_copy_file_writes_lf_for_text_even_when_source_is_crlf(tmp_path: Path) -> None:
    """When the wheel ships CRLF (Windows-built), the installed file should
    still land on disk as LF — Layer 3 of the EOL strategy."""
    from aexp.install import _copy_file

    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    src.write_bytes(b"line1\r\nline2\r\n")
    action = _copy_file(src, dst, force=False)
    assert action.kind == "copied"
    # The written file is LF only, regardless of source EOL convention.
    assert dst.read_bytes() == b"line1\nline2\n"


def test_copy_file_preserves_binary_bytes_unchanged(tmp_path: Path) -> None:
    """Binary files survive the install copy bit-for-bit."""
    from aexp.install import _copy_file

    src = tmp_path / "icon.png"
    dst = tmp_path / "out.png"
    payload = b"\x89PNG\r\n\x1a\n\x00\xff\r\nMORE"
    src.write_bytes(payload)
    action = _copy_file(src, dst, force=False)
    assert action.kind == "copied"
    assert dst.read_bytes() == payload  # CRLF bytes untouched


def test_reinstall_after_crlf_target_reports_identical(tmp_path: Path) -> None:
    """End-to-end: a re-install where the existing target is CRLF and the
    source is LF (or vice versa) reports skipped_identical, not
    skipped_conflict. This is the symptom that motivated the whole fix."""
    from aexp.install import _copy_file

    src = tmp_path / "shipped.md"
    dst = tmp_path / "installed.md"
    # Source ships LF (post-.gitattributes-fix wheel).
    src.write_bytes(b"hello\nworld\n")
    # Target on disk is CRLF (leftover from a pre-fix install, or because the
    # consumer's editor saved it that way).
    dst.write_bytes(b"hello\r\nworld\r\n")
    action = _copy_file(src, dst, force=False)
    assert action.kind == "skipped_identical"


def test_repo_root_gitattributes_forces_lf_for_text() -> None:
    """The repo's .gitattributes must declare LF for the text types we ship.

    Regression guard against silently dropping the file — without it, future
    wheels could re-introduce CRLF into the package data and the symptom
    would only surface on a fresh consumer install."""
    repo_root = Path(__file__).resolve().parents[1]
    ga = repo_root / ".gitattributes"
    assert ga.is_file(), f"missing .gitattributes at {ga}"
    body = ga.read_text(encoding="utf-8")
    # Each of these extensions ships in the package data (slash commands,
    # bundled docs, scaffold JSON). If any drift off, the install symptom
    # comes back.
    for ext in (".md", ".json", ".py", ".toml"):
        # Match either `*<ext> text eol=lf` or equivalent.
        pat_a = f"*{ext}"
        assert pat_a in body and "eol=lf" in body, (
            f"expected `*{ext}` line with `eol=lf` in .gitattributes"
        )


# ---------------------------------------------------------------------------
# .gitignore block-merge + machine_label
# ---------------------------------------------------------------------------


def test_install_writes_gitignore_block_on_fresh_repo(fresh_git_repo: Path) -> None:
    """Fresh install writes the managed .gitignore with the aexp block."""
    install_scaffold(fresh_git_repo)
    gi = (fresh_git_repo / ".gitignore").read_text(encoding="utf-8")
    assert "# agentic-experiments:begin" in gi
    assert "# agentic-experiments:end" in gi
    assert ".aexp/*" in gi
    assert "!.aexp/runs-index/" in gi
    assert "!.aexp/ledger/" in gi


def test_install_gitignore_is_idempotent(fresh_git_repo: Path) -> None:
    """Two installs in a row produce identical .gitignore content."""
    install_scaffold(fresh_git_repo)
    first = (fresh_git_repo / ".gitignore").read_text(encoding="utf-8")
    install_scaffold(fresh_git_repo, force=True)
    second = (fresh_git_repo / ".gitignore").read_text(encoding="utf-8")
    assert first == second


def test_install_gitignore_preserves_user_lines_outside_block(
    fresh_git_repo: Path,
) -> None:
    """User-authored gitignore entries above/below our block are preserved."""
    gi = fresh_git_repo / ".gitignore"
    gi.write_text(
        "# user content above\n*.log\n*.tmp\n\n# user content below\n",
        encoding="utf-8",
    )
    install_scaffold(fresh_git_repo)
    final = gi.read_text(encoding="utf-8")
    assert "# user content above" in final
    assert "*.log" in final
    assert "*.tmp" in final
    assert "# user content below" in final
    assert "# agentic-experiments:begin" in final


def test_install_gitignore_migration_warning_on_legacy_aexp_rule(
    fresh_git_repo: Path,
) -> None:
    """Legacy `.aexp/` rule outside our block emits a migration warning."""
    gi = fresh_git_repo / ".gitignore"
    gi.write_text(
        "# pre-existing aexp ignore — this is the legacy pattern\n.aexp/\n",
        encoding="utf-8",
    )
    actions = install_scaffold(fresh_git_repo)
    warning_actions = [a for a in actions if a.kind == "gitignore_migration_warning"]
    assert len(warning_actions) == 1, [a.kind for a in actions]
    assert ".aexp/" in warning_actions[0].detail


def test_install_gitignore_no_warning_when_legacy_rule_replaced(
    fresh_git_repo: Path,
) -> None:
    """A consumer who's already replaced their `.aexp/` rule with `.aexp/*`
    doesn't get a migration warning."""
    gi = fresh_git_repo / ".gitignore"
    gi.write_text("# pre-existing wildcard form\n.aexp/*\n", encoding="utf-8")
    actions = install_scaffold(fresh_git_repo)
    warning_actions = [a for a in actions if a.kind == "gitignore_migration_warning"]
    assert warning_actions == []


# machine_label behavior


def test_install_seeds_machine_label_from_hostname(fresh_git_repo: Path) -> None:
    """Fresh install populates machine_label with short hostname by default."""
    import socket

    install_scaffold(fresh_git_repo)
    from aexp.utils.paths import read_installed_marker

    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert "machine_label" in marker
    expected = socket.gethostname().split(".")[0]
    assert marker["machine_label"] == expected


def test_install_machine_label_override(fresh_git_repo: Path) -> None:
    """Explicit machine_label is written to the marker."""
    install_scaffold(fresh_git_repo, machine_label="cluster")
    from aexp.utils.paths import read_installed_marker

    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert marker["machine_label"] == "cluster"


def test_install_machine_label_sticky_across_reinstall(fresh_git_repo: Path) -> None:
    """Re-install without --machine-label preserves the previous value."""
    install_scaffold(fresh_git_repo, machine_label="cluster")
    install_scaffold(fresh_git_repo, force=True)  # no machine_label
    from aexp.utils.paths import read_installed_marker

    marker = read_installed_marker(fresh_git_repo)
    assert marker is not None
    assert marker["machine_label"] == "cluster"


def test_read_machine_label_helper_falls_back_to_hostname(tmp_path: Path) -> None:
    """read_machine_label() with no marker returns short hostname."""
    import socket

    from aexp.utils.paths import read_machine_label

    label = read_machine_label(tmp_path)
    assert label == (socket.gethostname().split(".")[0] or "unknown")


def test_read_machine_label_helper_reads_marker(fresh_git_repo: Path) -> None:
    """read_machine_label() returns the explicit value from the marker."""
    install_scaffold(fresh_git_repo, machine_label="cluster")
    from aexp.utils.paths import read_machine_label

    assert read_machine_label(fresh_git_repo) == "cluster"
