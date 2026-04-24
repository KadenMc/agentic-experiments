"""Tests for ``install_limina``."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aexp.install import (
    InstallAction,
    block_merge_markdown,
    compute_vendor_sha,
    install_limina,
    is_limina_installed,
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
    install_limina(fresh_git_repo)
    commands = fresh_git_repo / ".claude" / "commands"
    assert commands.is_dir()
    for name in (
        "aexp-new-run.md",
        "aexp-finding-from-run.md",
        "aexp-finding-from-batch.md",
        "aexp-finding-placeholder.md",
        "aexp-queue-add.md",
        "aexp-queue-materialize.md",
    ):
        assert (commands / name).is_file(), name


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
    # Cross-platform invocation fields written by default.
    assert "python_exe" in marker
    assert Path(marker["python_exe"]).exists()
    # conda_env_name is present (may be "" when running under a venv).
    assert "conda_env_name" in marker
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

    actions = install_limina(fresh_git_repo)
    entries = [a for a in actions if a.path.endswith("kb/ACTIVE.md")]
    assert entries
    assert entries[0].kind == "preserved_user_modified"
    # User content untouched.
    assert target.read_text(encoding="utf-8") == "user-owned content"


def test_install_preserves_user_modified_kb_content_even_under_force(
    fresh_git_repo: Path,
) -> None:
    """Under ``--force``, user-authored ``kb/`` scaffold files still survive.

    This is the regression guard for the 2026-04-24 electricrag CHALLENGE.md
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

    actions = install_limina(fresh_git_repo, force=True)
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

    actions = install_limina(fresh_git_repo, force=True)
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
    install_limina(fresh_git_repo)  # first run lays down the defaults
    actions = install_limina(fresh_git_repo, force=True)
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

    actions = install_limina(fresh_git_repo, force=True)
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

    install_limina(fresh_git_repo)
    merged = json.loads((claude / "settings.json").read_text("utf-8"))
    # User's hook survived
    post_tool = merged["hooks"]["PostToolUse"]
    commands = [h["command"] for group in post_tool for h in group["hooks"]]
    assert "user-bash.sh" in commands
    # Vendor's hook got appended
    assert any("aexp.hooks.kb_write_guard" in c for c in commands)
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
    """After install, the seeded kb/ passes structural validation."""
    from aexp.kb_validate import validate_kb

    install_limina(fresh_git_repo)
    result = validate_kb(fresh_git_repo / "kb")
    assert result.ok, result.errors


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
        "installed_skill",
        "wrote_marker",
        "already_installed",
    }
    for a in actions:
        assert isinstance(a, InstallAction)
        assert a.kind in valid_kinds, a
        assert a.path, a


def test_install_copies_limina_skills_to_claude_skills(fresh_git_repo: Path) -> None:
    """All vendored Limina skills must land under <repo>/.claude/skills/.

    Without these, the AGENTS.md references like $experiment-rigor are broken
    on every consumer repo.
    """
    install_limina(fresh_git_repo)
    skills_root = fresh_git_repo / ".claude" / "skills"
    assert skills_root.is_dir()
    # Top-level "limina" skill (from vendor/limina/skill/)
    assert (skills_root / "limina" / "SKILL.md").is_file()
    # Research-methodology skills (from vendor/limina/skills/)
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
    actions = install_limina(fresh_git_repo)
    skill_actions = [a for a in actions if a.kind == "installed_skill"]
    # 4 research skills + 1 top-level "limina" skill = 5 installed_skill entries.
    assert len(skill_actions) == 5, [a.path for a in skill_actions]
    paths = {Path(a.path).name for a in skill_actions}
    assert "limina" in paths
    assert "experiment-rigor" in paths


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


def test_install_writes_mcp_json_at_repo_root(fresh_git_repo: Path) -> None:
    """``.mcp.json`` at the repo root must contain an ``mcpServers.aexp`` entry.

    This is the file Claude Code reads for project-scope MCP servers;
    ``.claude/settings.json`` does NOT drive MCP config.
    """
    install_limina(fresh_git_repo)
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
    install_limina(fresh_git_repo)
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
    install_limina(fresh_git_repo)
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
    """``install_limina(..., dev=True)`` writes a direct-Python MCP invocation
    so editable installs (``pip install -e``) take effect on the MCP surface.
    """
    import sys

    install_limina(fresh_git_repo, dev=True)
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
    install_limina(fresh_git_repo, dev=True)
    mcp_dev = json.loads((fresh_git_repo / ".mcp.json").read_text("utf-8"))
    assert mcp_dev["mcpServers"]["aexp"]["command"] != "uvx"

    install_limina(fresh_git_repo, force=True, dev=False)
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
    install_limina(fresh_git_repo)
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
    install_limina(fresh_git_repo)
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
    install_limina(fresh_git_repo)
    mcp = json.loads(user_mcp.read_text("utf-8"))
    combined = (
        [mcp["mcpServers"]["aexp"]["command"]]
        + mcp["mcpServers"]["aexp"]["args"]
    )
    assert "aexp-mcp-server" in combined
    assert "stale_module" not in combined
