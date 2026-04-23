"""Install the vendored Limina harness into a consumer repo.

``install_limina`` walks the vendored snapshot at
``src/aexp/vendor/limina/`` and applies it to a target repo:

- ``kb/``, ``templates/``, ``scripts/`` -> copied verbatim (skipped if the
  target already has identical content; conflicting target files are skipped
  with a warning unless ``force=True``).
- ``claude_settings.json`` -> JSON-merged into ``<repo>/.claude/settings.json``.
- ``AGENTS.md``, ``CLAUDE.md`` -> block-merged with begin/end markers if the
  target already exists; copied otherwise.
- Signac project initialized at the requested run-store path.
- Install marker written to ``<repo>/.aexp/installed.json``.

The function returns a list of ``InstallAction`` records describing every
file touched — useful for CLI output and tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import signac

from aexp import __version__
from aexp.utils.atomic import atomic_write
from aexp.utils.paths import (
    INSTALLED_MARKER_REL,
    read_installed_marker,
    write_installed_marker,
)

VENDOR_LIMINA = Path(__file__).resolve().parent / "vendor" / "limina"

# Subdirectories of the vendor tree that get copied verbatim into the consumer repo.
#
# Intentionally does NOT include ``scripts/``. Hook scripts, kb_validate, and
# other package code live inside ``aexp.*`` and are invoked via
# ``<python_exe> -m aexp.hooks.<name>`` — they upgrade through
# ``pip install -U agentic-experiments`` rather than by re-running install.
# The consumer repo ends up with kb/ data and templates they can edit, plus
# the generated .mcp.json / .claude/settings.json / .aexp/installed.json —
# no Python code they did not write.
_TREES_VERBATIM: tuple[str, ...] = ("kb", "templates")

# Top-level files that get merged (not copied) when the target already exists.
_MERGE_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")

# Block-merge markers used for AGENTS.md / CLAUDE.md append behavior.
_BEGIN_MARKER = "<!-- agentic-experiments:begin -->"
_END_MARKER = "<!-- agentic-experiments:end -->"

# Limina's Claude Code skills get copied into <repo>/.claude/skills/<name>/ so
# they travel with the repo. AGENTS.md references them as $<name>; without
# this step those references would be broken.
#
# The vendored ``skill/`` (singular) is the top-level "limina" skill; the
# vendored ``skills/*`` (plural) are the four research-methodology skills
# (experiment-rigor, exploratory-sota-research, research-devil-advocate,
# build-maintainable-software).
_SKILL_TOPLEVEL_NAME = "limina"


ActionKind = Literal[
    "copied",
    "skipped_identical",
    "skipped_conflict",
    "merged_json",
    "merged_block",
    "initialized_runs",
    "installed_skill",
    "wrote_marker",
    "already_installed",
]


@dataclass(frozen=True)
class InstallAction:
    """A single side-effect recorded by ``install_limina``."""

    kind: ActionKind
    path: str  # relative to repo root
    detail: str = ""


# ---------------------------------------------------------------------------
# Vendor-tree fingerprinting
# ---------------------------------------------------------------------------


def compute_vendor_sha(vendor_root: Path = VENDOR_LIMINA) -> str:
    """Compute a deterministic hash of every file under ``vendor_root``.

    Files are sorted by POSIX-style relative path, then hashed as
    ``<relpath>\\0<bytes>\\0`` into a SHA-256. Two installations with the
    same vendor tree produce identical hashes regardless of OS.
    """
    h = hashlib.sha256()
    files = sorted(p for p in vendor_root.rglob("*") if p.is_file())
    for p in files:
        rel = p.relative_to(vendor_root).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------


def _files_identical(a: Path, b: Path) -> bool:
    """Bytewise comparison; returns ``False`` if either side is missing."""
    if not a.is_file() or not b.is_file():
        return False
    return a.read_bytes() == b.read_bytes()


def _copy_file(src: Path, dst: Path, *, force: bool, dry_run: bool = False) -> InstallAction:
    """Copy ``src`` -> ``dst`` atomically, respecting existing-file conflicts.

    Rules
    -----
    - Target missing -> copy, record ``copied``.
    - Target identical -> skip, record ``skipped_identical``.
    - Target differs + ``force=False`` -> skip, record ``skipped_conflict``.
    - Target differs + ``force=True`` -> overwrite, record ``copied``.

    ``dry_run=True`` suppresses the actual write while still returning the
    planned action — callers can preview the full side-effect list safely.
    """
    rel = _display_relpath(dst)
    if _files_identical(src, dst):
        return InstallAction("skipped_identical", rel)
    if dst.exists() and not force:
        return InstallAction(
            "skipped_conflict",
            rel,
            "target exists with different content; rerun with force=True to overwrite",
        )
    if not dry_run:
        atomic_write(dst, src.read_bytes())
    return InstallAction("copied", rel)


def _display_relpath(path: Path) -> str:
    """Return POSIX-style relative path string for display; falls back to str."""
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# .claude/settings.json JSON-merge
# ---------------------------------------------------------------------------


def merge_claude_settings(
    vendor_settings: dict[str, Any],
    existing_settings: dict[str, Any],
) -> dict[str, Any]:
    """Merge our hook block into an existing Claude Code settings dict.

    - If a top-level key (e.g. ``"hooks"``, ``"permissions"``) is absent in the
      existing settings, it is copied from the vendor settings.
    - Within ``hooks`` (the only block we care about today), matchers from the
      vendor are appended to matchers from the user; identical (matcher,
      command) pairs are deduplicated.
    - Non-``hooks`` user keys are preserved untouched.
    """
    # Deep-copy both sides so we never mutate the caller's dicts; the caller
    # relies on the original ``existing_settings`` remaining intact for the
    # post-merge "did anything change?" equality check.
    merged: dict[str, Any] = copy.deepcopy(existing_settings)
    vendor_hooks = copy.deepcopy(vendor_settings.get("hooks", {}))
    existing_hooks = merged.get("hooks", {})

    for event, vendor_matchers in vendor_hooks.items():
        existing_matchers = existing_hooks.get(event, [])
        # Build a set of (matcher, command) pairs already present to dedupe.
        seen: set[tuple[str, str]] = set()
        for group in existing_matchers:
            matcher = group.get("matcher", "")
            for h in group.get("hooks", []):
                seen.add((matcher, h.get("command", "")))

        combined = list(existing_matchers)
        for vgroup in vendor_matchers:
            matcher = vgroup.get("matcher", "")
            new_hooks = [
                h
                for h in vgroup.get("hooks", [])
                if (matcher, h.get("command", "")) not in seen
            ]
            if not new_hooks:
                continue
            # Add a fresh group with only the new hooks.
            combined.append({"matcher": matcher, "hooks": new_hooks})
            for h in new_hooks:
                seen.add((matcher, h.get("command", "")))
        existing_hooks[event] = combined

    if existing_hooks:
        merged["hooks"] = existing_hooks

    # Any other top-level keys from the vendor that the user does not have get copied.
    # Note: we never write mcpServers to this file — that belongs in .mcp.json.
    for k, v in vendor_settings.items():
        if k == "hooks":
            continue
        if k not in merged:
            merged[k] = v

    return merged


def _build_claude_settings(python_exe: str) -> dict[str, Any]:
    """Build the ``.claude/settings.json`` hook block that ``aexp`` manages.

    Each hook invokes a Python module inside the installed ``aexp`` package
    via the recorded interpreter path (``{python_exe} -m aexp.hooks.<name>``).
    This means hooks upgrade with ``pip install -U agentic-experiments`` —
    no re-running ``aexp install``, no stale script copies in the consumer
    repo.

    ``python_exe`` is quoted with double quotes so paths containing spaces
    (e.g. ``C:\\Program Files\\...``) work under every shell Claude Code
    might spawn.
    """
    def cmd(mod: str) -> str:
        return f'"{python_exe}" -m aexp.hooks.{mod}'

    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": cmd("session_start"), "timeout": 15}
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit",
                    "hooks": [
                        {"type": "command", "command": cmd("enforce_hef_chain"), "timeout": 5}
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit",
                    "hooks": [
                        {"type": "command", "command": cmd("kb_write_guard"), "timeout": 15}
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": cmd("stop_validate"), "timeout": 30}
                    ],
                }
            ],
        }
    }


def _merge_or_write_claude_settings(
    dst: Path, python_exe: str, *, dry_run: bool = False
) -> InstallAction:
    """Write (or merge) our hook block into ``<repo>/.claude/settings.json``.

    Preserves any existing user hooks, permissions, and other top-level keys;
    only appends our hook matchers (deduplicating on ``(matcher, command)``).
    """
    rel = _display_relpath(dst)
    vendor = _build_claude_settings(python_exe)

    if not dst.exists():
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(dst, json.dumps(vendor, indent=2) + "\n")
        return InstallAction("copied", rel)

    try:
        existing = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return InstallAction(
            "skipped_conflict",
            rel,
            f"existing {dst.name} is not valid JSON ({exc}); leaving untouched",
        )

    merged = merge_claude_settings(vendor, existing)

    if merged == existing:
        return InstallAction("skipped_identical", rel)

    if not dry_run:
        atomic_write(dst, json.dumps(merged, indent=2) + "\n")
    return InstallAction("merged_json", rel)


def _merge_or_write_mcp_json(
    dst: Path, repo_root: Path, *, dry_run: bool = False, dev: bool = False
) -> InstallAction:
    """Write (or merge) our MCP server entry into ``<repo>/.mcp.json``.

    Claude Code reads project-scope MCP servers from ``.mcp.json`` at the
    repo root — *not* from ``.claude/settings.json``. Default form is
    portable across machines (``uvx`` / PyPI). Pass ``dev=True`` to use the
    current interpreter instead — lets editable installs take effect on
    the MCP side (at the cost of a machine-specific ``.mcp.json``).
    """
    rel = _display_relpath(dst)
    our_entry = {"aexp": _build_mcp_server_entry(repo_root, dev=dev)}
    payload = {"mcpServers": our_entry}

    if not dst.exists():
        if not dry_run:
            atomic_write(dst, json.dumps(payload, indent=2) + "\n")
        return InstallAction("copied", rel)

    try:
        existing = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return InstallAction(
            "skipped_conflict",
            rel,
            f"existing {dst.name} is not valid JSON ({exc}); leaving untouched",
        )
    if not isinstance(existing, dict):
        return InstallAction(
            "skipped_conflict",
            rel,
            f"existing {dst.name} is not a JSON object; leaving untouched",
        )

    merged = copy.deepcopy(existing)
    merged.setdefault("mcpServers", {})
    # Always refresh our own entry; preserve any user-defined servers.
    merged["mcpServers"]["aexp"] = our_entry["aexp"]

    if merged == existing:
        return InstallAction("skipped_identical", rel)

    if not dry_run:
        atomic_write(dst, json.dumps(merged, indent=2) + "\n")
    return InstallAction("merged_json", rel)


def _build_mcp_server_entry(repo_root: Path, *, dev: bool = False) -> dict[str, Any]:
    """Compose the ``mcpServers.aexp`` entry for ``.mcp.json``.

    Two forms, depending on the intended workflow.

    **Default (``dev=False``) — portable uvx invocation.**
    The canonical pattern for Python MCP servers (used by every reference
    server under ``modelcontextprotocol/servers``)::

        uvx --from agentic-experiments[mcp] aexp-mcp-server

    - Single ``.mcp.json`` works on every machine with ``uv`` installed
      (no absolute Python paths, no env names baked in) — safe to commit
      to git so teammates get the server on clone.
    - ``uvx`` fetches ``agentic-experiments`` from PyPI on first use and
      caches it under ``~/.cache/uv``; subsequent starts are fast.
    - Sidesteps the Windows Claude Code stdio bugs (#29443 et al.):
      ``uvx.exe`` is spawned directly, no shell wrapper, no conda
      activation pipe buffering.

    **Dev mode (``dev=True``) — direct env Python.**
    Invokes the MCP server through the current interpreter, using the
    locally-installed ``aexp`` package (editable or otherwise)::

        "<python_exe>" -m aexp.mcp_server

    This is what you want when you're *developing* ``aexp`` and need
    edits to ``src/aexp/mcp_server.py`` (or any module it imports) to
    flow through to Claude Code. The trade-off: the generated
    ``.mcp.json`` hard-codes your machine's Python path, so it is
    **not** safe to commit as-is — gitignore it while iterating.

    ``PYTHONUNBUFFERED=1`` is set on both forms as belt + suspenders for
    stdio flushing on Windows.
    """
    if dev:
        import sys as _sys
        return {
            "command": _sys.executable,
            "args": ["-m", "aexp.mcp_server"],
            "env": {"PYTHONUNBUFFERED": "1"},
        }
    return {
        "command": "uvx",
        "args": [
            "--from",
            "agentic-experiments[mcp]",
            "aexp-mcp-server",
        ],
        "env": {"PYTHONUNBUFFERED": "1"},
    }


# ---------------------------------------------------------------------------
# AGENTS.md / CLAUDE.md block-merge
# ---------------------------------------------------------------------------


def block_merge_markdown(existing: str, vendor: str) -> str:
    """Append (or refresh) a vendor-managed block inside ``existing``.

    If the begin/end markers are already present, replace whatever is between
    them. Otherwise, append the vendor content wrapped in markers.
    """
    begin = _BEGIN_MARKER
    end = _END_MARKER
    block = f"{begin}\n{vendor.rstrip()}\n{end}\n"

    if begin in existing and end in existing:
        before, rest = existing.split(begin, 1)
        _, after = rest.split(end, 1)
        return f"{before}{block}{after.lstrip()}"

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return f"{existing}{separator}{block}"


def _merge_or_copy_markdown(src: Path, dst: Path, *, dry_run: bool = False) -> InstallAction:
    """Copy or block-merge a Markdown file based on whether target exists."""
    rel = _display_relpath(dst)
    vendor_text = src.read_text(encoding="utf-8")

    if not dst.exists():
        if not dry_run:
            atomic_write(dst, vendor_text)
        return InstallAction("copied", rel)

    existing_text = dst.read_text(encoding="utf-8")
    merged = block_merge_markdown(existing_text, vendor_text)
    if merged == existing_text:
        return InstallAction("skipped_identical", rel)

    if not dry_run:
        atomic_write(dst, merged)
    return InstallAction("merged_block", rel)


# ---------------------------------------------------------------------------
# Tree-copy helpers
# ---------------------------------------------------------------------------


def _install_skills(root: Path, *, force: bool, dry_run: bool = False) -> list[InstallAction]:
    """Copy vendored Limina skills into ``<root>/.claude/skills/``.

    - ``vendor/limina/skill/`` (singular, the top-level "limina" skill) →
      ``<root>/.claude/skills/limina/``
    - ``vendor/limina/skills/<name>/`` (each research skill) →
      ``<root>/.claude/skills/<name>/``

    Each installed skill emits one ``installed_skill`` action. File-level
    conflicts (existing skill files differing from vendor) are handled per
    the same rules as ``_copy_tree``.
    """
    actions: list[InstallAction] = []
    dst_skills = root / ".claude" / "skills"

    top_src = VENDOR_LIMINA / "skill"
    if top_src.is_dir():
        dst = dst_skills / _SKILL_TOPLEVEL_NAME
        tree_actions = _copy_tree(top_src, dst, force=force, dry_run=dry_run)
        actions.extend(tree_actions)
        if any(a.kind == "copied" for a in tree_actions):
            actions.append(
                InstallAction(
                    "installed_skill",
                    _display_relpath(dst),
                    f"copied vendor/limina/skill -> {_display_relpath(dst)}",
                )
            )

    skills_src = VENDOR_LIMINA / "skills"
    if skills_src.is_dir():
        for skill_dir in sorted(p for p in skills_src.iterdir() if p.is_dir()):
            dst = dst_skills / skill_dir.name
            tree_actions = _copy_tree(skill_dir, dst, force=force, dry_run=dry_run)
            actions.extend(tree_actions)
            if any(a.kind == "copied" for a in tree_actions):
                actions.append(
                    InstallAction(
                        "installed_skill",
                        _display_relpath(dst),
                        f"copied vendor/limina/skills/{skill_dir.name} -> {_display_relpath(dst)}",
                    )
                )

    return actions


def _copy_tree(
    src_root: Path, dst_root: Path, *, force: bool, dry_run: bool = False
) -> list[InstallAction]:
    """Copy every file under ``src_root`` into ``dst_root``."""
    actions: list[InstallAction] = []
    if not src_root.is_dir():
        return actions
    for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        actions.append(_copy_file(src, dst, force=force, dry_run=dry_run))
    return actions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_limina(
    repo_root: str | Path,
    *,
    run_store: str = ".runs",
    force: bool = False,
    assert_git: bool = True,
    dry_run: bool = False,
    dev: bool = False,
) -> list[InstallAction]:
    """Install the vendored Limina harness into ``repo_root``.

    Parameters
    ----------
    repo_root : str or Path
        Absolute path to the consumer repo root.
    run_store : str, optional
        Relative path where the signac project should live. Default ``".runs"``.
    force : bool, optional
        Overwrite files that already exist with different content. Also skips
        the ``already installed`` short-circuit.
    assert_git : bool, optional
        If ``True`` (default), require ``repo_root`` to contain a ``.git`` dir;
        pass ``False`` in tests that want to install into a plain folder.
    dry_run : bool, optional
        Compute and return the full action plan without actually writing any
        files or touching signac. Callers can preview the side effects safely
        and then re-invoke with ``dry_run=False`` to commit.
    dev : bool, optional
        Use a **development** form of ``.mcp.json`` that invokes the MCP
        server via the current Python interpreter (``"<python_exe>" -m
        aexp.mcp_server``) instead of the default ``uvx``/PyPI form. The
        dev form honours editable installs (``pip install -e``) so source
        edits flow through to the MCP surface — at the cost of baking a
        machine-specific path into ``.mcp.json``. Do not commit the file
        to git while using this mode.

    Returns
    -------
    list[InstallAction]
        Chronological record of every path touched (or that *would* be
        touched, under ``dry_run=True``).

    Raises
    ------
    FileNotFoundError
        If ``repo_root`` does not exist.
    NotADirectoryError
        If ``repo_root`` exists but is not a directory.
    RuntimeError
        If ``assert_git=True`` and ``repo_root/.git`` is missing.
    """
    root = Path(repo_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"repo_root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"repo_root is not a directory: {root}")
    if assert_git and not (root / ".git").exists():
        raise RuntimeError(
            f"repo_root is not a git repo: {root}. "
            "Run `git init` first or pass assert_git=False."
        )

    actions: list[InstallAction] = []

    # Short-circuit if already installed at the same vendor sha.
    vendor_sha = compute_vendor_sha()
    existing_marker = read_installed_marker(root)
    if existing_marker and not force:
        if existing_marker.get("limina_vendor_sha") == vendor_sha:
            actions.append(
                InstallAction(
                    "already_installed",
                    str(INSTALLED_MARKER_REL.as_posix()),
                    f"vendor sha {vendor_sha[:12]} already applied at "
                    f"{existing_marker.get('installed_at', 'unknown')}",
                )
            )
            return actions

    # 1. Copy verbatim trees.
    for name in _TREES_VERBATIM:
        actions.extend(
            _copy_tree(VENDOR_LIMINA / name, root / name, force=force, dry_run=dry_run)
        )

    # 2. Copy / block-merge top-level Markdown docs.
    for name in _MERGE_FILES:
        src = VENDOR_LIMINA / name
        if not src.is_file():
            continue
        actions.append(_merge_or_copy_markdown(src, root / name, dry_run=dry_run))

    # 3a. Write (or JSON-merge) our hook block into .claude/settings.json.
    #     Hooks run the installed aexp package via the current interpreter
    #     (`{python_exe} -m aexp.hooks.<name>`), so we need the interpreter
    #     path locked in before we generate the command strings.
    #     (mcpServers is NOT read from this file by Claude Code — it lives
    #     in .mcp.json at repo root; see step 3c.)
    import sys as _sys
    claude_dst = root / ".claude" / "settings.json"
    actions.append(
        _merge_or_write_claude_settings(claude_dst, _sys.executable, dry_run=dry_run)
    )

    # 3c. Write project-scope MCP servers to .mcp.json at repo root.
    #     This is the file Claude Code actually reads for project-scope
    #     servers (shared with the team via version control, unless
    #     ``dev=True`` — see docstring).
    actions.append(
        _merge_or_write_mcp_json(root / ".mcp.json", root, dry_run=dry_run, dev=dev)
    )

    # 3b. Install Limina's Claude Code skills into <repo>/.claude/skills/.
    # AGENTS.md references skills like $experiment-rigor; without this step
    # those references are broken for every consumer repo.
    actions.extend(_install_skills(root, force=force, dry_run=dry_run))

    # 4. Initialize signac project.
    run_store_path = (root / run_store).resolve()
    if not dry_run:
        _ensure_signac_project(run_store_path)
    actions.append(
        InstallAction("initialized_runs", run_store, f"signac project at {run_store_path}")
    )

    # 5. Write install marker.
    if dry_run:
        actions.append(InstallAction("wrote_marker", str(INSTALLED_MARKER_REL.as_posix())))
        return actions
    marker_path = write_installed_marker(
        root,
        version=__version__,
        run_store_path=run_store,
        limina_vendor_sha=vendor_sha,
    )
    actions.append(InstallAction("wrote_marker", _display_relpath(marker_path)))

    return actions


def _ensure_signac_project(path: Path) -> signac.Project:
    """Idempotently initialize a signac project rooted at ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        return signac.get_project(path=str(path))
    except LookupError:
        return signac.init_project(path=str(path))


def is_limina_installed(repo_root: str | Path) -> bool:
    """True if ``repo_root`` has an install marker AND the expected tree shape.

    We check for ``kb/`` and ``.claude/settings.json`` — the two consumer-repo
    artifacts that ``aexp install`` always produces. Hook scripts no longer
    land in the consumer repo (they live in the installed ``aexp`` package),
    so the old ``scripts/hooks/`` check was dropped.
    """
    root = Path(repo_root)
    marker = read_installed_marker(root)
    if marker is None:
        return False
    return (root / "kb").is_dir() and (root / ".claude" / "settings.json").is_file()
