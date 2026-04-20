"""Install the vendored Limina harness into a consumer repo.

``install_limina`` walks the vendored snapshot at
``src/agentic_experiments/vendor/limina/`` and applies it to a target repo:

- ``kb/``, ``templates/``, ``scripts/`` -> copied verbatim (skipped if the
  target already has identical content; conflicting target files are skipped
  with a warning unless ``force=True``).
- ``claude_settings.json`` -> JSON-merged into ``<repo>/.claude/settings.json``.
- ``AGENTS.md``, ``CLAUDE.md`` -> block-merged with begin/end markers if the
  target already exists; copied otherwise.
- Signac project initialized at the requested run-store path.
- Install marker written to ``<repo>/.agentic_experiments/installed.json``.

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

from agentic_experiments import __version__
from agentic_experiments.utils.atomic import atomic_write
from agentic_experiments.utils.paths import (
    INSTALLED_MARKER_REL,
    read_installed_marker,
    write_installed_marker,
)

VENDOR_LIMINA = Path(__file__).resolve().parent / "vendor" / "limina"

# Subdirectories of the vendor tree that get copied verbatim into the consumer repo.
_TREES_VERBATIM: tuple[str, ...] = ("kb", "templates", "scripts")

# Top-level files that get merged (not copied) when the target already exists.
_MERGE_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")

# Block-merge markers used for AGENTS.md / CLAUDE.md append behavior.
_BEGIN_MARKER = "<!-- agentic-experiments:begin -->"
_END_MARKER = "<!-- agentic-experiments:end -->"


ActionKind = Literal[
    "copied",
    "skipped_identical",
    "skipped_conflict",
    "merged_json",
    "merged_block",
    "initialized_runs",
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


def _copy_file(src: Path, dst: Path, *, force: bool) -> InstallAction:
    """Copy ``src`` -> ``dst`` atomically, respecting existing-file conflicts.

    Rules
    -----
    - Target missing -> copy, record ``copied``.
    - Target identical -> skip, record ``skipped_identical``.
    - Target differs + ``force=False`` -> skip, record ``skipped_conflict``.
    - Target differs + ``force=True`` -> overwrite, record ``copied``.
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
    for k, v in vendor_settings.items():
        if k == "hooks":
            continue
        if k not in merged:
            merged[k] = v

    return merged


def _merge_or_write_json(src: Path, dst: Path) -> InstallAction:
    """Write ``src`` verbatim if ``dst`` is missing, else JSON-merge into it."""
    rel = _display_relpath(dst)
    if not dst.exists():
        atomic_write(dst, src.read_bytes())
        return InstallAction("copied", rel)

    try:
        existing = json.loads(dst.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return InstallAction(
            "skipped_conflict",
            rel,
            f"existing {dst.name} is not valid JSON ({exc}); leaving untouched",
        )

    vendor = json.loads(src.read_text(encoding="utf-8"))
    merged = merge_claude_settings(vendor, existing)

    if merged == existing:
        return InstallAction("skipped_identical", rel)

    atomic_write(dst, json.dumps(merged, indent=2) + "\n")
    return InstallAction("merged_json", rel)


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


def _merge_or_copy_markdown(src: Path, dst: Path) -> InstallAction:
    """Copy or block-merge a Markdown file based on whether target exists."""
    rel = _display_relpath(dst)
    vendor_text = src.read_text(encoding="utf-8")

    if not dst.exists():
        atomic_write(dst, vendor_text)
        return InstallAction("copied", rel)

    existing_text = dst.read_text(encoding="utf-8")
    merged = block_merge_markdown(existing_text, vendor_text)
    if merged == existing_text:
        return InstallAction("skipped_identical", rel)

    atomic_write(dst, merged)
    return InstallAction("merged_block", rel)


# ---------------------------------------------------------------------------
# Tree-copy helpers
# ---------------------------------------------------------------------------


def _copy_tree(
    src_root: Path, dst_root: Path, *, force: bool
) -> list[InstallAction]:
    """Copy every file under ``src_root`` into ``dst_root``."""
    actions: list[InstallAction] = []
    if not src_root.is_dir():
        return actions
    for src in sorted(p for p in src_root.rglob("*") if p.is_file()):
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        actions.append(_copy_file(src, dst, force=force))
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

    Returns
    -------
    list[InstallAction]
        Chronological record of every path touched.

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
        actions.extend(_copy_tree(VENDOR_LIMINA / name, root / name, force=force))

    # 2. Copy / block-merge top-level Markdown docs.
    for name in _MERGE_FILES:
        src = VENDOR_LIMINA / name
        if not src.is_file():
            continue
        actions.append(_merge_or_copy_markdown(src, root / name))

    # 3. JSON-merge into .claude/settings.json.
    claude_src = VENDOR_LIMINA / "claude_settings.json"
    claude_dst = root / ".claude" / "settings.json"
    if claude_src.is_file():
        actions.append(_merge_or_write_json(claude_src, claude_dst))

    # 4. Initialize signac project.
    run_store_path = (root / run_store).resolve()
    _ensure_signac_project(run_store_path)
    actions.append(
        InstallAction("initialized_runs", run_store, f"signac project at {run_store_path}")
    )

    # 5. Write install marker.
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
    """True if ``repo_root`` has an install marker AND the vendor tree is present."""
    root = Path(repo_root)
    marker = read_installed_marker(root)
    if marker is None:
        return False
    return (root / "kb").is_dir() and (root / "scripts" / "hooks").is_dir()
