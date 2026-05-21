"""Install the vendored Limina harness into a consumer repo.

``install_scaffold`` walks the vendored snapshot at
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
import re
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


class InstallRefused(RuntimeError):
    """Raised by :func:`install_scaffold` when install must not proceed.

    Currently only raised by the source-tree-self-install guard (refusing
    to install a consumer-side scaffold inside the agentic-experiments
    package's own source tree). The CLI catches this and exits with a
    rich-printed message; programmatic callers can branch on it.
    """


# Regex for detecting the agentic-experiments source tree's pyproject.toml.
# Matches `name = "agentic-experiments"` (or single-quoted) in any context
# of the file. Text-based rather than TOML-parsed so the guard runs before
# any import-heavy machinery and stays dependency-light.
_AEXP_SOURCE_NAME_RE = re.compile(
    r'^\s*name\s*=\s*["\']agentic-experiments["\']\s*$',
    re.MULTILINE,
)


def _find_aexp_source_tree(start: Path) -> Path | None:
    """Walk up from ``start`` looking for the agentic-experiments source tree.

    Returns the absolute path of the directory containing a
    ``pyproject.toml`` whose ``[project].name`` is ``"agentic-experiments"``,
    or ``None`` if no such pyproject is found in the walk-up chain.

    The walk-up is deliberate so an invocation from any subdirectory of
    the source tree (e.g. ``src/``, ``tests/``) is also detected, not
    just the root.

    No legitimate consumer would set this name in their pyproject, so a
    match is unambiguous evidence that the caller is pointing
    ``install`` at the dev repo itself — almost always a mistake (the
    install would materialize a Limina/signac consumer scaffold inside
    the package source tree).
    """
    cur = start.resolve()
    while True:
        pyp = cur / "pyproject.toml"
        if pyp.is_file():
            try:
                content = pyp.read_text(encoding="utf-8")
            except OSError:
                return None
            if _AEXP_SOURCE_NAME_RE.search(content):
                return cur
        if cur.parent == cur:
            return None
        cur = cur.parent

VENDOR_ROOT = Path(__file__).resolve().parent / "vendor" / "limina"

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


ActionKind = Literal[
    "copied",
    "skipped_identical",
    "skipped_conflict",
    "preserved_user_modified",
    "merged_json",
    "merged_block",
    "initialized_runs",
    "installed_skill",
    "wrote_marker",
    "already_installed",
]


@dataclass(frozen=True)
class InstallAction:
    """A single side-effect recorded by ``install_scaffold``."""

    kind: ActionKind
    path: str  # relative to repo root
    detail: str = ""


# ---------------------------------------------------------------------------
# Vendor-tree fingerprinting
# ---------------------------------------------------------------------------


def compute_vendor_sha(vendor_root: Path = VENDOR_ROOT) -> str:
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


# Text file extensions whose line endings we normalize on read/write.
# Binary files (images, archives, the signac state dir, etc.) are NOT in this
# set — they're compared bytewise and copied verbatim.
_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {".md", ".json", ".py", ".toml", ".yaml", ".yml", ".txt", ".rst",
     ".csv", ".cfg", ".ini", ".sh", ".gitignore", ".gitattributes"}
)


def _is_text_file(path: Path) -> bool:
    """True if ``path`` should be treated as text for EOL normalization.

    We key on file extension rather than content sniffing because the source
    side of every install copy is a known set of vendored package files —
    we already know which are text.
    """
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name in {".gitignore", ".gitattributes"}


def _eol_normalize(data: bytes) -> bytes:
    """Collapse CRLF (and lone CR) into LF so cross-platform copies compare equal.

    The wheel format preserves source-tree byte sequences verbatim, so a wheel
    built on Windows with ``core.autocrlf=true`` ships CRLF inside the package
    and a consumer checkout with LF on disk will byte-differ from it forever.
    Normalizing on both sides of the equality check makes the comparison
    semantic rather than literal. See ``docs/setup/jupyter-mcp.md`` and the
    .gitattributes file at repo root for the broader strategy.
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _files_identical(a: Path, b: Path) -> bool:
    """EOL-normalized comparison for text files; bytewise for everything else.

    Returns ``False`` if either side is missing.

    For text files (``_is_text_file``), CRLF and LF are treated as equivalent —
    this is what stops a CRLF-on-Windows source vs. LF-on-Linux target from
    looking like a "user customization" to the installer.
    """
    if not a.is_file() or not b.is_file():
        return False
    raw_a = a.read_bytes()
    raw_b = b.read_bytes()
    if raw_a == raw_b:
        return True
    # Fast-path fell through. Only EOL-normalize for known text files; on
    # binary mismatch we never want to claim equality.
    if _is_text_file(a) and _is_text_file(b):
        return _eol_normalize(raw_a) == _eol_normalize(raw_b)
    return False


def _copy_file(
    src: Path,
    dst: Path,
    *,
    force: bool,
    dry_run: bool = False,
    preserve_user_modifications: bool = False,
) -> InstallAction:
    """Copy ``src`` -> ``dst`` atomically, respecting existing-file conflicts.

    Rules
    -----
    - Target missing -> copy, record ``copied``.
    - Target identical to source -> skip, record ``skipped_identical``.
    - Target differs + ``force=False`` -> skip, record ``skipped_conflict``.
    - Target differs + ``force=True`` + ``preserve_user_modifications=False``
      -> overwrite, record ``copied``.
    - Target differs + ``force=True`` + ``preserve_user_modifications=True``
      -> skip, record ``preserved_user_modified``. This is how
      user-authored content in the ``kb/`` + ``templates/`` scaffold
      survives a re-install: when the installer sees a file it shipped
      as a stub that's been edited on disk, it leaves the edit alone
      even under ``--force``. Tooling files (slash commands, skills,
      hooks) set the flag to ``False`` so legitimate refreshes go
      through.

    ``dry_run=True`` suppresses the actual write while still returning the
    planned action — callers can preview the full side-effect list safely.
    """
    rel = _display_relpath(dst)
    if _files_identical(src, dst):
        return InstallAction("skipped_identical", rel)
    if dst.exists() and preserve_user_modifications:
        return InstallAction(
            "preserved_user_modified",
            rel,
            "target has user-authored content (differs from shipped default); "
            "preserved. `rm` the file before re-installing if you want to reset.",
        )
    if dst.exists() and not force:
        return InstallAction(
            "skipped_conflict",
            rel,
            "target exists with different content; rerun with force=True to overwrite",
        )
    if not dry_run:
        # Text files: write with LF line endings regardless of what the
        # wheel actually ships. This is the belt-and-suspenders layer that
        # paves over a CRLF-laden wheel built from a Windows dev tree before
        # the .gitattributes normalization took effect.
        raw = src.read_bytes()
        if _is_text_file(src):
            raw = _eol_normalize(raw)
        atomic_write(dst, raw)
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


def _build_claude_settings(
    python_exe: str, *, jupyter_enabled: bool = False
) -> dict[str, Any]:
    """Build the ``.claude/settings.json`` hook block that ``aexp`` manages.

    Each hook invokes a Python module inside the installed ``aexp`` package
    via the recorded interpreter path (``{python_exe} -m aexp.hooks.<name>``).
    This means hooks upgrade with ``pip install -U agentic-experiments`` —
    no re-running ``aexp install``, no stale script copies in the consumer
    repo.

    ``python_exe`` is quoted with double quotes so paths containing spaces
    (e.g. ``C:\\Program Files\\...``) work under every shell Claude Code
    might spawn.

    When ``jupyter_enabled=True``, also registers a PostToolUse matcher on
    ``mcp__jupyter.*__connect_to_jupyter`` that nudges the agent to re-run
    :func:`aexp.jupyter.init` immediately after any port switch. Without
    this, an agent that calls ``connect_to_jupyter`` mid-conversation may
    keep reasoning under stale identity beliefs.
    """
    def cmd(mod: str) -> str:
        return f'"{python_exe}" -m aexp.hooks.{mod}'

    posttooluse: list[dict[str, Any]] = [
        {
            "matcher": "Write|Edit|MultiEdit",
            "hooks": [
                {"type": "command", "command": cmd("kb_write_guard"), "timeout": 15}
            ],
        }
    ]
    if jupyter_enabled:
        posttooluse.append(
            {
                "matcher": "mcp__jupyter.*__connect_to_jupyter",
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd("jupyter_connect_postuse"),
                        "timeout": 5,
                    }
                ],
            }
        )

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
            "PostToolUse": posttooluse,
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
    dst: Path, python_exe: str, *, dry_run: bool = False, jupyter_enabled: bool = False
) -> InstallAction:
    """Write (or merge) our hook block into ``<repo>/.claude/settings.json``.

    Preserves any existing user hooks, permissions, and other top-level keys;
    only appends our hook matchers (deduplicating on ``(matcher, command)``).
    """
    rel = _display_relpath(dst)
    vendor = _build_claude_settings(python_exe, jupyter_enabled=jupyter_enabled)

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
    dst: Path,
    repo_root: Path,
    *,
    dry_run: bool = False,
    dev: bool = False,
    with_jupyter: bool = False,
) -> InstallAction:
    """Write (or merge) our MCP server entries into ``<repo>/.mcp.json``.

    Claude Code reads project-scope MCP servers from ``.mcp.json`` at the
    repo root — *not* from ``.claude/settings.json``. Default form is
    portable across machines (``uvx`` / PyPI). Pass ``dev=True`` to use the
    current interpreter instead — lets editable installs take effect on
    the MCP side (at the cost of a machine-specific ``.mcp.json``).

    When ``with_jupyter=True``, also writes the ``jupyter`` entry used by
    the Jupyter MCP integration. The entry is *additive*: once written,
    subsequent installs without the flag leave it in place (matching the
    "never delete user-defined servers" pattern). To back out, the user
    edits ``.mcp.json`` by hand.
    """
    rel = _display_relpath(dst)
    our_entries: dict[str, Any] = {"aexp": _build_mcp_server_entry(repo_root, dev=dev)}
    if with_jupyter:
        our_entries.update(_jupyter_mcp_entries())
    payload = {"mcpServers": our_entries}

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
    # Always refresh our own ``aexp`` entry; preserve any user-defined servers.
    merged["mcpServers"]["aexp"] = our_entries["aexp"]
    # Jupyter entry: only ever ADD. If the user already has a `jupyter`
    # block (from a prior --with-jupyter install or a manual setup) leave
    # it alone — they may have customized the URL/port or pinned a
    # version, which we must not clobber.
    if with_jupyter and "jupyter" not in merged["mcpServers"]:
        merged["mcpServers"]["jupyter"] = our_entries["jupyter"]

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


def _jupyter_mcp_entries() -> dict[str, Any]:
    """MCP server entry for the Jupyter MCP integration.

    A single laptop-side server:

    - ``jupyter`` — laptop-side ``uvx jupyter-mcp-server`` running in
      MCP_SERVER mode (stdio to Claude, HTTP+WS to the remote Jupyter).
      The target Jupyter URL + token are supplied per-session at runtime
      via the ``connect_to_jupyter`` tool, so no token lives in this
      entry and the *same* entry retargets to any node — open a tunnel on
      a new local port, call ``connect_to_jupyter`` at the new URL, done.
      No ``.mcp.json`` edit, no MCP restart. That runtime retargeting is
      what makes the multi-node workflow (``/aexp-jupyter-connect`` /
      ``/aexp-jupyter-discover``) work.

    **Why ``jupyter-mcp-server`` is pinned to ``==0.23.0``.**
    ``jupyter-mcp-server`` v1.0.0 (released 2026-04-03) made
    server-startup auth mandatory: it reads ``JUPYTER_URL`` /
    ``JUPYTER_TOKEN`` / ``MCP_TOKEN`` from the *environment when the
    process starts*. Claude Code spawns this server over stdio with no
    such env block, so on v1.0.x the process comes up but never
    completes the MCP handshake — Claude Code shows the ``jupyter``
    server stuck "connecting" forever, exposing no tools.

    Moving *forward* to v1.0.x is not a fix here: the cluster JupyterLab
    URL + token rotate every compute-node session, so baking them into
    ``.mcp.json`` as static startup env vars is the wrong model. This
    integration is built on the *runtime* ``connect_to_jupyter(
    jupyter_url, jupyter_token)`` call, which the pre-auth 0.23.0 line
    supports cleanly. 0.23.0 is the last release before the
    mandatory-auth change and is the version verified against the
    electricrag deployment (2026-05-15).

    The pin is load-bearing: an *unpinned* ``jupyter-mcp-server``
    resolves to "latest" via ``uvx`` — currently v1.0.x — so an unpinned
    entry ships broken. Revisit the pin only when v1.0.x grows a
    runtime-retarget path (or stdio-spawn stops requiring startup env);
    if you bump it, also update the ``.mcp.json`` example and
    "Environment reference" in ``docs/setup/jupyter-mcp.md``.
    """
    return {
        "jupyter": {
            "command": "uvx",
            # Pinned deliberately -- v1.0.x's mandatory startup-env auth
            # hangs the MCP stdio handshake. Full rationale in the
            # docstring above; do not unpin without re-verifying.
            "args": ["jupyter-mcp-server==0.23.0"],
        },
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
    """Copy the vendored research-methodology skills into ``<root>/.claude/skills/``.

    Each ``vendor/limina/skills/<name>/`` directory is copied to
    ``<root>/.claude/skills/<name>/`` and emits one ``installed_skill``
    action. File-level conflicts (existing skill files differing from
    vendor) are handled per the same rules as ``_copy_tree``.
    """
    actions: list[InstallAction] = []
    dst_skills = root / ".claude" / "skills"

    skills_src = VENDOR_ROOT / "skills"
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


SLASH_COMMANDS_SRC = Path(__file__).resolve().parent / "slash_commands"


def _install_slash_commands(
    root: Path, *, target_rel: str = ".claude/commands", force: bool, dry_run: bool = False
) -> list[InstallAction]:
    """Copy shipped slash commands into ``<root>/<target_rel>/``.

    Produces ``copied`` / ``skipped_identical`` / ``skipped_conflict`` actions
    through the same ``_copy_file`` helper as every other install step, so the
    summary rolls them up cleanly and ``--force`` / dry-run behaviour is
    consistent with the rest of ``aexp install``.
    """
    actions: list[InstallAction] = []
    if not SLASH_COMMANDS_SRC.is_dir():
        return actions
    dst_dir = root / target_rel
    for md in sorted(SLASH_COMMANDS_SRC.glob("*.md")):
        actions.append(_copy_file(md, dst_dir / md.name, force=force, dry_run=dry_run))
    return actions


def _copy_tree(
    src_root: Path,
    dst_root: Path,
    *,
    force: bool,
    dry_run: bool = False,
    preserve_user_modifications: bool = False,
) -> list[InstallAction]:
    """Copy every file under ``src_root`` into ``dst_root``.

    ``preserve_user_modifications=True`` opts the whole tree into the
    content-diff preservation path (see :func:`_copy_file`) — targets
    that have diverged from the shipped source are preserved under
    ``--force``. Used for ``kb/`` + ``templates/`` where files ship as
    editable scaffold rather than pinned tooling.
    """
    actions: list[InstallAction] = []
    if not src_root.is_dir():
        return actions
    for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        actions.append(
            _copy_file(
                src,
                dst,
                force=force,
                dry_run=dry_run,
                preserve_user_modifications=preserve_user_modifications,
            )
        )
    return actions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_scaffold(
    repo_root: str | Path,
    *,
    run_store: str = ".runs",
    force: bool = False,
    assert_git: bool = True,
    dry_run: bool = False,
    dev: bool = False,
    allow_self_install: bool = False,
    with_jupyter: bool = False,
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
    allow_self_install : bool, optional
        If ``False`` (default), refuse to install when ``repo_root`` (or
        any ancestor) is the agentic-experiments source tree itself —
        detected by walking up looking for ``pyproject.toml`` with
        ``name = "agentic-experiments"``. This catches the common
        footgun of running ``poetry -C <aexp-repo> run aexp install``
        from a separate scratch directory: Poetry's ``-C`` swaps the
        subprocess cwd to the project, so the install ends up
        materializing the consumer scaffold inside the dev repo
        instead of the user's intended target. Pass ``True`` to
        override (e.g. dogfooding the consumer scaffold against the
        dev repo on purpose).
    with_jupyter : bool, optional
        If ``True``, also write the ``jupyter`` MCP server entry into
        ``.mcp.json``, vendor ``docs/setup/jupyter-mcp.md`` into the
        consumer repo, and set ``jupyter_enabled: true`` in the install
        marker. The marker bit is sticky — once set, subsequent installs
        preserve it even if ``with_jupyter=False``. The ``.mcp.json``
        entry is additive: an existing user-defined ``jupyter`` block is
        preserved (so a customized URL/port survives).
        See ``docs/setup/jupyter-mcp.md`` for the full setup recipe.

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
    InstallRefused
        If ``allow_self_install=False`` and ``repo_root`` resolves into
        the agentic-experiments source tree.
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

    # Source-tree self-install guard. Runs *before* any filesystem writes
    # so a refused install leaves the source tree completely untouched.
    if not allow_self_install:
        src_tree = _find_aexp_source_tree(root)
        if src_tree is not None:
            raise InstallRefused(
                f"refusing to install into the agentic-experiments source "
                f"tree ({src_tree}).\n\n"
                f"`aexp install` materializes a consumer-side scaffold "
                f"(kb/, templates/, .claude/, .runs/, etc.) — running it "
                f"inside the package's own source tree pollutes the dev "
                f"repo with non-package files and creates a Limina/signac "
                f"project layered on top of itself.\n\n"
                f"You almost certainly meant to install into a separate "
                f"consumer repo. `cd` there and re-run.\n\n"
                f"Common cause: `poetry -C <aexp-repo> run aexp install` "
                f"from a scratch directory — Poetry's `-C` swaps the "
                f"subprocess cwd to the project, so the install lands in "
                f"the dev repo instead of where you `cd`'d. Invoke the "
                f"venv's `aexp` executable directly (or use `--directory` "
                f"with care) to keep cwd at your intended target.\n\n"
                f"If you really need to install into this tree (e.g. "
                f"dogfooding the consumer scaffold against the dev repo), "
                f"pass --allow-self-install (CLI) or "
                f"allow_self_install=True (Python API)."
            )

    actions: list[InstallAction] = []

    # Short-circuit if already installed at the same vendor sha.
    vendor_sha = compute_vendor_sha()
    existing_marker = read_installed_marker(root)
    # The `jupyter_enabled` marker bit is sticky: a user who once opted in
    # should keep getting the Jupyter PostToolUse hook on subsequent installs
    # even if they omit --with-jupyter. OR the request with the existing
    # value before deciding what to register.
    effective_jupyter = with_jupyter or bool(
        (existing_marker or {}).get("jupyter_enabled", False)
    )
    if existing_marker and not force:
        # Dual-read: markers written before the de-brand carry the legacy
        # `limina_vendor_sha` key. Fall back to it so an old marker still
        # short-circuits cleanly instead of forcing a spurious re-install.
        marker_sha = existing_marker.get("vendor_sha") or existing_marker.get(
            "limina_vendor_sha"
        )
        if marker_sha == vendor_sha:
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
    #    kb/ + templates/ ship as *editable scaffold*, not pinned tooling:
    #    CHALLENGE.md / ACTIVE.md / DASHBOARD.md / templates/*.md are all
    #    meant to be authored or customised by the consumer. So even under
    #    --force, any target that has diverged from the shipped default is
    #    preserved (see `_copy_file` rules). Tooling files — slash commands,
    #    skills, hooks — opt out of preservation below and get refreshed.
    for name in _TREES_VERBATIM:
        actions.extend(
            _copy_tree(
                VENDOR_ROOT / name,
                root / name,
                force=force,
                dry_run=dry_run,
                preserve_user_modifications=True,
            )
        )

    # 2. Copy / block-merge top-level Markdown docs.
    for name in _MERGE_FILES:
        src = VENDOR_ROOT / name
        if not src.is_file():
            continue
        actions.append(_merge_or_copy_markdown(src, root / name, dry_run=dry_run))

    # 2a. Vendor the Jupyter MCP setup doc to docs/setup/jupyter-mcp.md.
    #     Unlike kb/ + templates/ (editable scaffold), this is a canonical
    #     reference doc that ships fixes via `pip install -U`. We use the
    #     standard tooling-file rules (overwrite under --force, skip
    #     conflict otherwise) — NOT preserve_user_modifications. Project-
    #     specific overlay info belongs in a sibling file like
    #     docs/setup/jupyter-mcp-local.md.
    #
    #     Copied unconditionally (not gated on --with-jupyter): the doc is
    #     small, harmless, and lets a consumer read about the integration
    #     before deciding to opt in.
    jupyter_doc_src = VENDOR_ROOT / "docs" / "setup" / "jupyter-mcp.md"
    if jupyter_doc_src.is_file():
        actions.append(
            _copy_file(
                jupyter_doc_src,
                root / "docs" / "setup" / "jupyter-mcp.md",
                force=force,
                dry_run=dry_run,
            )
        )

    # 3a. Write (or JSON-merge) our hook block into .claude/settings.json.
    #     Hooks run the installed aexp package via the current interpreter
    #     (`{python_exe} -m aexp.hooks.<name>`), so we need the interpreter
    #     path locked in before we generate the command strings.
    #     (mcpServers is NOT read from this file by Claude Code — it lives
    #     in .mcp.json at repo root; see step 3c.)
    import sys as _sys
    claude_dst = root / ".claude" / "settings.json"
    actions.append(
        _merge_or_write_claude_settings(
            claude_dst,
            _sys.executable,
            dry_run=dry_run,
            jupyter_enabled=effective_jupyter,
        )
    )

    # 3c. Write project-scope MCP servers to .mcp.json at repo root.
    #     This is the file Claude Code actually reads for project-scope
    #     servers (shared with the team via version control, unless
    #     ``dev=True`` — see docstring).
    actions.append(
        _merge_or_write_mcp_json(
            root / ".mcp.json",
            root,
            dry_run=dry_run,
            dev=dev,
            with_jupyter=with_jupyter,
        )
    )

    # 3b. Install Limina's Claude Code skills into <repo>/.claude/skills/.
    # AGENTS.md references skills like $experiment-rigor; without this step
    # those references are broken for every consumer repo.
    actions.extend(_install_skills(root, force=force, dry_run=dry_run))

    # 3d. Install aexp's slash commands into <repo>/.claude/commands/.
    # Previously this was an opt-in ``aexp install-slash-commands`` second
    # step — easy to miss, no good reason to keep separate. Now part of the
    # standard flow; the standalone verb remains for re-installs to a custom
    # target directory.
    actions.extend(_install_slash_commands(root, force=force, dry_run=dry_run))

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
        vendor_sha=vendor_sha,
        jupyter_enabled=with_jupyter,
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


def is_scaffold_installed(repo_root: str | Path) -> bool:
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
