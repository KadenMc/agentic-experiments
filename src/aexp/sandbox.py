"""Sandbox directory scaffolding for exploratory notebook work.

This module is the implementation behind ``/aexp-new-sandbox``. It creates
a per-experiment subdirectory under ``notebooks/_sandbox/`` (or a
caller-supplied parent), seeded with a README, a ``helpers.py`` skeleton,
and an optional initial notebook scaffold. On first use, it also
initializes the sandbox root (``notebooks/_sandbox/`` + its README +
``.gitignore``).

A sandbox is *not* a tracked research artifact (no ``H###``/``E###``
allocated, no ``kb_write_guard`` validation). It's a free-form
exploratory workspace whose conventions are encoded by the scaffolder.
Promote a sandbox experiment to the tracked-artifact graph with
``/aexp-promote-nb`` once a directional hypothesis lands.

The sandbox convention this module installs:

- ``notebooks/_sandbox/<YYYY-MM-DD>_<slug>/`` per-experiment subdir
- Per-subdir ``README.md`` (experiment-design template)
- Per-subdir ``helpers.py`` (sandbox-local utilities; freely editable)
- Top-level ``notebooks/_sandbox/README.md`` + ``.gitignore`` (created
  on first sandbox creation if absent)

The sandbox autonomy boundary (notebook-cells autonomous; package code
git-dance; canon explicit-permission) is documented in the scaffolded
README but enforced socially, not by aexp tooling.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as date_cls
from pathlib import Path

from aexp.utils.atomic import atomic_write
from aexp.utils.paths import find_repo_root

# ---------------------------------------------------------------------------
# Constants + helpers
# ---------------------------------------------------------------------------

DEFAULT_SANDBOX_ROOT = "notebooks/_sandbox"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DATE_SLUG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([a-z0-9][a-z0-9-]*)$")


class SandboxScaffoldError(RuntimeError):
    """Raised when sandbox scaffolding fails a pre-condition."""


@dataclass(frozen=True)
class SandboxScaffoldResult:
    """Returned by :func:`scaffold`."""

    slug: str
    dir_name: str   # e.g. "2026-05-11_my-experiment"
    dir_path: str   # repo-relative POSIX path to the per-experiment dir
    files_created: list[str] = field(default_factory=list)
    root_initialized: bool = False  # True if we also created sandbox root README/.gitignore


def slugify(title: str, *, max_len: int = 60) -> str:
    """Lowercase, hyphenate, strip non-alnum — produces a filesystem-safe slug.

    Empty or all-punctuation titles fall back to ``"untitled"`` so the caller
    still gets a valid filename; callers that care should validate their input
    before calling.

    Mirrors :func:`aexp.artifacts.slugify` so the same input produces
    identical slugs across artifact and sandbox scaffolding.
    """
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    if not s:
        return "untitled"
    return s[:max_len].rstrip("-") or "untitled"


def setup_sandbox_notebook(name: str, *, start: Path | str | None = None) -> dict:
    """Boot a sandbox notebook: discover repo root + sandbox subdir, set ``sys.path``.

    Designed to be the first cell of any sandbox notebook::

        from aexp.sandbox import setup_sandbox_notebook
        ctx = setup_sandbox_notebook("2026-05-11_my-experiment")
        import helpers

    Handles the F4 friction: kernel ``cwd`` on a remote Jupyter server is the
    notebook's directory, not the repo root, so naive ``Path("notebooks/...").resolve()``
    doubles the path. We walk upward from ``start`` (default ``cwd``) to find
    the git repo root, then locate the sandbox subdir under it.

    Parameters
    ----------
    name : str
        The sandbox subdirectory name (e.g. ``"2026-05-11_my-experiment"``).
    start : Path, str, or None
        Where to start the upward walk to find the repo root. Defaults to
        ``Path.cwd()``.

    Returns
    -------
    dict
        ``{"repo_root": Path, "sandbox_dir": Path}``. The sandbox dir is
        inserted at the front of ``sys.path`` so ``import helpers`` resolves
        to this experiment's ``helpers.py``.

    Raises
    ------
    FileNotFoundError
        If the named sandbox subdir doesn't exist under ``<repo>/notebooks/_sandbox/``.
    """
    repo_root = find_repo_root(start=start)
    sandbox_dir = repo_root / DEFAULT_SANDBOX_ROOT / name
    if not sandbox_dir.is_dir():
        raise FileNotFoundError(
            f"sandbox subdir not found: {sandbox_dir}. Did you create it with "
            f"`aexp new-sandbox --slug ...`?"
        )
    if str(sandbox_dir) not in sys.path:
        sys.path.insert(0, str(sandbox_dir))
    return {"repo_root": repo_root, "sandbox_dir": sandbox_dir}


# ---------------------------------------------------------------------------
# Inline templates
# ---------------------------------------------------------------------------
# Templates are inline strings rather than separate files. v0 trade-off:
# inline = easier to ship + reason about; cost = users can't override
# without monkey-patching. If override needs surface, extract to
# scaffold/templates/sandbox/ and add a precedence rule similar to
# artifacts._load_template.

_SANDBOX_ROOT_README = """# notebooks/_sandbox/

Agent-driven exploratory work. Each subdirectory is one experiment-attempt,
tracked in git so the working notebooks evolve visibly over time.

This directory is **autonomous-write for the agent**: notebook cells and
kernel-written outputs inside any subdir under here don't require
per-edit permission. Everything outside this directory (package code,
configs, `kb/` artifacts, canon docs) is *not* autonomous — those are
either git-dance with the user in the loop, or explicit-permission
territory.

When a sandbox experiment becomes paper-load-bearing, promote it to a
tracked artifact via the aexp slash command sequence:

1. `/aexp-new-thread` (if no parent thread yet)
2. `/aexp-new-hypothesis --thread T###`
3. `/aexp-new-experiment --hypothesis H###`
4. `/aexp-promote-nb` to extract working cells into a tracked-run script

## Convention

- `<YYYY-MM-DD>_<short-slug>/` — one subdir per experiment-attempt.
- Multiple notebooks per subdir are OK; name them sensibly
  (`00_feasibility.ipynb`, `01_calibration.ipynb`, etc.).
- `helpers.py` for experiment-local utilities (may be multiple files).
- Small outputs (plots, CSVs, JSONs) are tracked. Large outputs
  (`*.npy`, `*.parquet`, `*.h5`, anything under `outputs/large/`) are
  gitignored via the sibling `.gitignore`.

Created by `aexp new-sandbox`. This file is regenerated only if it
doesn't already exist; safe to edit by hand once created.
"""

_SANDBOX_ROOT_GITIGNORE = """# Sandbox-local exclusions for large outputs.
# Tracked: notebooks, helpers, small CSVs, plots.
# Gitignored: anything matching the patterns below.

**/*.npy
**/*.parquet
**/*.h5
**/*.feather
**/outputs/large/**
**/.ipynb_checkpoints/
"""

_PER_EXPERIMENT_README = """# {dir_name} — {title}

**Mode:** exploratory (sandbox; not paper-cite-able until promoted to
`H### → E### → F###`).

**Origin context:** _add a pointer to the session note or motivation
that triggered this experiment._

## Statement (directional — no thresholds; refine via calibration)

_What's the claim? Keep it directional, not threshold-locked: predict an
ordering (e.g., ``A > B > C``) without committing to magnitudes until
calibration data is in hand._

## Mechanism

_Architectural / theoretical reason it might hold. Cite specific
artifacts (subagent findings, prior findings, paper sections) by file
path; don't extrapolate from general knowledge._

## Why this might generalize

_Why should this hold beyond the current eval slice or wording?_

## Shortcut risks

_What could make this look good without improving the real capability?
List ≥3 risks with mitigations._

## Test plan

_What runs are needed; what conditions are compared; how the
val/test split is structured. Reference the sandbox helpers + any
existing infrastructure._

## Stages

### Stage 0 — feasibility

_Cheapest first pass; verify the building blocks load + produce
non-degenerate output before scaling._

### Stage 1+

_Subsequent stages as needed._

## Open questions

_Things to surface for the user when results come in._

## Cross-references

- _Session note path_
- _Related threads / hypotheses / findings_
- _Helper module: `helpers.py`_
"""

_PER_EXPERIMENT_HELPERS_PY = '''"""Sandbox-local helpers for {dir_name}.

This file is owned by this experiment subdir. Edit freely; not shared
with other sandbox experiments. If a helper here ends up being reused
across experiments, consider promoting it to a real package module
(via git-dance with user in the loop).

Boilerplate provides:
- ``SANDBOX_DIR`` — absolute path to this directory
- ``REPO_ROOT`` — absolute path to the enclosing git repo

Notebook usage::

    # First cell:
    from aexp.sandbox import setup_sandbox_notebook
    ctx = setup_sandbox_notebook("{dir_name}")
    import helpers

The ``setup_sandbox_notebook`` call handles ``sys.path`` so ``import
helpers`` resolves to *this* file, and discovers the repo root robustly
(handles the kernel-cwd-vs-repo-root trap on remote Jupyter setups).
"""
from __future__ import annotations

from pathlib import Path

from aexp.utils.paths import find_repo_root

SANDBOX_DIR = Path(__file__).resolve().parent
REPO_ROOT = find_repo_root(start=SANDBOX_DIR)


# ---------------------------------------------------------------------------
# Add your experiment-specific helpers below this line.
# ---------------------------------------------------------------------------
'''


# ---------------------------------------------------------------------------
# Internal scaffolding helpers
# ---------------------------------------------------------------------------


def _write_if_absent(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if the file doesn't already exist.

    Returns True if a new file was written, False if it already existed.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content)
    return True


def _relative_posix(path: Path, *, repo_root: Path) -> str:
    """Return ``path`` as a POSIX-style string.

    Tries relative-to-repo_root first (the typical case when parent_dir
    is under the repo). Falls back to the absolute path if the file
    lives outside the repo (rare; happens when the caller passes an
    absolute ``parent_dir`` outside the repo — e.g. for tests).
    """
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
        return rel.as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _today_iso(today: date_cls | None = None) -> str:
    """ISO date string used in the sandbox dir name."""
    return (today or datetime.now(UTC).date()).isoformat()


def _validate_slug(slug: str) -> None:
    if not slug:
        raise SandboxScaffoldError("slug is required and cannot be empty")
    cleaned = slugify(slug)
    if cleaned != slug:
        raise SandboxScaffoldError(
            f"slug {slug!r} contains non-slug characters. "
            f"Try {cleaned!r} instead (lowercase, hyphen-separated, alnum-only)."
        )


def _resolve_repo_root(repo_root: str | Path | None) -> Path:
    if repo_root is None:
        return find_repo_root()
    return Path(repo_root).resolve()


def _resolve_sandbox_root(
    repo_root: Path, parent_dir: str | Path | None
) -> Path:
    """Compute the sandbox-root directory (where per-experiment subdirs live).

    Default: ``<repo_root>/notebooks/_sandbox/``. Caller may override via
    ``parent_dir`` (relative paths resolve under repo_root; absolute paths
    are used as-is).
    """
    if parent_dir is None:
        return repo_root / DEFAULT_SANDBOX_ROOT
    p = Path(parent_dir)
    if p.is_absolute():
        return p
    return repo_root / p


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scaffold(
    slug: str,
    *,
    title: str | None = None,
    repo_root: str | Path | None = None,
    parent_dir: str | Path | None = None,
    today: date_cls | None = None,
) -> SandboxScaffoldResult:
    """Scaffold a new sandbox experiment subdirectory.

    Creates ``<parent_dir>/<YYYY-MM-DD>_<slug>/`` populated with a
    per-experiment ``README.md`` + ``helpers.py``. If the sandbox root
    (``parent_dir``) doesn't yet contain a top-level README and
    ``.gitignore``, those are created too.

    Parameters
    ----------
    slug : str
        Filesystem-safe slug describing the experiment. Will validate
        against the slugify rules; raises if input contains non-slug chars.
    title : str or None
        Human-readable title for the per-experiment README's H1. Defaults
        to ``slug.replace("-", " ").title()`` if not provided.
    repo_root : str, Path, or None
        Repo root override. Defaults to walking up from cwd to find a
        ``.git`` directory (via :func:`aexp.utils.paths.find_repo_root`).
    parent_dir : str, Path, or None
        Sandbox-root override. Defaults to ``<repo_root>/notebooks/_sandbox``.
        Relative paths resolve under repo_root.
    today : datetime.date or None
        Date override for the directory prefix. Defaults to today (UTC).
        Useful for tests + reproducible scaffolding.

    Returns
    -------
    SandboxScaffoldResult
        Records the dir name + path + list of files created + whether
        the sandbox root was initialized.

    Raises
    ------
    SandboxScaffoldError
        If ``slug`` is empty / invalid, or if the target experiment
        subdir already exists.
    """
    _validate_slug(slug)
    if title is None:
        title = slug.replace("-", " ").title()

    repo = _resolve_repo_root(repo_root)
    sandbox_root = _resolve_sandbox_root(repo, parent_dir)
    date_str = _today_iso(today)
    dir_name = f"{date_str}_{slug}"
    target_dir = sandbox_root / dir_name

    if target_dir.exists():
        raise SandboxScaffoldError(
            f"target sandbox subdir already exists: {target_dir}. "
            f"Pick a different slug or delete the existing dir."
        )

    files_created: list[str] = []
    root_initialized = False

    # Step 1: ensure sandbox root has README + .gitignore (created on first use)
    root_readme = sandbox_root / "README.md"
    root_gitignore = sandbox_root / ".gitignore"
    if _write_if_absent(root_readme, _SANDBOX_ROOT_README):
        files_created.append(_relative_posix(root_readme, repo_root=repo))
        root_initialized = True
    if _write_if_absent(root_gitignore, _SANDBOX_ROOT_GITIGNORE):
        files_created.append(_relative_posix(root_gitignore, repo_root=repo))
        root_initialized = True

    # Step 2: create the per-experiment subdir
    target_dir.mkdir(parents=True, exist_ok=False)

    # Step 3: write per-experiment files
    per_readme = target_dir / "README.md"
    per_helpers = target_dir / "helpers.py"

    readme_body = _PER_EXPERIMENT_README.format(dir_name=dir_name, title=title)
    helpers_body = _PER_EXPERIMENT_HELPERS_PY.format(dir_name=dir_name)

    atomic_write(per_readme, readme_body)
    files_created.append(_relative_posix(per_readme, repo_root=repo))
    atomic_write(per_helpers, helpers_body)
    files_created.append(_relative_posix(per_helpers, repo_root=repo))

    return SandboxScaffoldResult(
        slug=slug,
        dir_name=dir_name,
        dir_path=_relative_posix(target_dir, repo_root=repo),
        files_created=files_created,
        root_initialized=root_initialized,
    )
