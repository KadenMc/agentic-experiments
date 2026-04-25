"""Create Limina ``kb/`` artifacts (H/E/F) with bidirectional backlinks.

This is the v1.1 surface flagged as planned in ``aexp.limina_io``: rather
than hand-rolling markdown from templates per slash-command invocation,
agents call :func:`new_hypothesis` / :func:`new_experiment` /
:func:`new_finding` and get a validator-clean file on disk plus every
required parent backlink patched in.

Responsibilities:

- Allocate the next unused ``H###`` / ``E###`` / ``F###`` id by scanning the
  kb tree.
- Slugify the user-supplied title for the filename.
- Render the shipped template (or the repo-local override at
  ``templates/<kind>.md`` if present) with the artifact id, date, title, and
  a ``## Links`` block pre-populated with every link ``kb_validate`` requires.
- Write the artifact atomically.
- Patch each parent artifact's ``## Links`` section via
  :func:`aexp.backlinks.add_backlink`.

Every verb returns an :class:`ArtifactCreateResult` so CLI/MCP callers can
surface the created path, id, and list of parent files they patched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aexp.backlinks import add_backlink
from aexp.limina_io import (
    ArtifactNotFoundError,
    find_artifact_path,
    kind_dir,
)
from aexp.schema import ArtifactKind
from aexp.utils.atomic import atomic_write
from aexp.utils.paths import find_repo_root

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPLATE_FILENAMES: dict[ArtifactKind, str] = {
    "H": "hypothesis.md",
    "E": "experiment.md",
    "F": "finding.md",
}

# Placeholder keys replaced by the renderer. Lower-case ``{command}``,
# ``{date}``, ``{step}`` etc. in the shipped templates are literal example
# text (e.g. "Confirm if: `{command}` -> ...") and must be left intact.
_PLACEHOLDER_RE = re.compile(
    r"\{(ARTIFACT_ID|TITLE|DATE|HYPOTHESIS_ID|EXPERIMENT_ID|IMPACT|LINKS_BLOCK)\}"
)

_ALWAYS_LINK = ("ACTIVE", "CHALLENGE")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ID_FROM_FILENAME_RE = re.compile(r"^(H|E|F|L|CR|SR)(\d{3})-")

# Where vendored templates live (fall-back if the consumer repo has no
# ``templates/`` directory yet). Mirrors the layout install.py copies from.
_VENDOR_TEMPLATES = (
    Path(__file__).resolve().parent / "vendor" / "limina" / "templates"
)


class ArtifactCreateError(RuntimeError):
    """Raised when artifact creation fails a pre-condition."""


@dataclass(frozen=True)
class ArtifactCreateResult:
    """Returned by :func:`new_hypothesis` / ``new_experiment`` / ``new_finding``."""

    artifact_id: str
    path: str  # repo-relative POSIX
    backlinks_patched: list[str] = field(default_factory=list)
    backlinks_already_present: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(title: str, *, max_len: int = 60) -> str:
    """Lowercase, hyphenate, strip non-alnum — produces a filesystem-safe slug.

    Empty or all-punctuation titles fall back to ``"untitled"`` so the caller
    still gets a valid filename; callers that care should validate their input
    before calling.
    """
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    if not s:
        return "untitled"
    return s[:max_len].rstrip("-") or "untitled"


def next_artifact_id(kind: ArtifactKind, *, kb_root: Path) -> str:
    """Return the smallest unused ``{kind}###`` id under ``kb_root``.

    Matches the convention already documented in the close-run slash command
    ("use the smallest unused F###") — avoids leaving gaps.
    """
    directory = kind_dir(kind, kb_root)
    used: set[int] = set()
    if directory.is_dir():
        pattern = re.compile(rf"^{kind}(\d{{3}})-")
        for p in directory.glob(f"{kind}*.md"):
            m = pattern.match(p.name)
            if m:
                used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"{kind}{n:03d}"


def _today_iso() -> str:
    """Date string formatted to match the existing templates."""
    return datetime.now(UTC).date().isoformat()


def _load_template(kind: ArtifactKind, *, repo_root: Path) -> str:
    """Return the canonical template text for an artifact kind.

    Always reads from the package-shipped templates at
    ``src/aexp/vendor/limina/templates/`` — the same source the
    validator's ``missing_template_header`` check uses
    (:mod:`aexp.kb_validate`). Single source of truth means creation
    and validation can never disagree about "what the template is."

    The repo-local ``<repo>/templates/`` directory is a *reference copy*
    populated (and preserved across re-installs) by ``aexp install``,
    but it's NEVER consulted by the artifact-creation API. Editing a
    local template will not affect what ``aexp.new_*`` renders.

    Per-project template overrides aren't supported yet. If you need
    one, file an issue describing the use case — likely shape is a
    ``--template-file <path>`` flag on the CLI verbs and an explicit
    ``template_path=`` kwarg on the Python API. The previous
    implicit "local file = override" semantic was removed because it
    silently broke for any consumer whose install predated a template
    change (creation would render the stale local skeleton; validation
    would reject it for missing the new shipped headers).

    The ``repo_root`` parameter is preserved for API stability but is
    intentionally unused.
    """
    filename = _TEMPLATE_FILENAMES[kind]
    return (_VENDOR_TEMPLATES / filename).read_text(encoding="utf-8")


def _render_template(tpl: str, substitutions: dict[str, str]) -> str:
    """Replace ``{KEY}`` placeholders without disturbing non-key braces."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        return substitutions.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(_sub, tpl)


def _render_links_block(targets: list[str]) -> str:
    """Render ``- [[X]]`` bullets, one per target, joined by newlines."""
    return "\n".join(f"- [[{t}]]" for t in targets)


def _artifact_exists(artifact_id: str, *, kb_root: Path) -> bool:
    try:
        find_artifact_path(artifact_id, kb_root=kb_root)
        return True
    except ArtifactNotFoundError:
        return False


def _relative_posix(path: Path, *, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _write_artifact(
    *,
    kind: ArtifactKind,
    kb_root: Path,
    repo_root: Path,
    artifact_id: str,
    slug: str,
    rendered: str,
) -> Path:
    directory = kind_dir(kind, kb_root)
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{artifact_id}-{slug}.md"
    if dest.exists():
        raise ArtifactCreateError(
            f"refusing to overwrite existing artifact at {dest}"
        )
    atomic_write(dest, rendered)
    return dest


def _patch_parents(
    *,
    parent_ids: list[str],
    child_id: str,
    kb_root: Path,
    repo_root: Path,
) -> tuple[list[str], list[str]]:
    """Add ``[[child_id]]`` to each parent's ``## Links`` section.

    Returns ``(patched, already_present)`` — both lists of repo-relative
    POSIX paths, so the caller can report which parents were touched.
    """
    patched: list[str] = []
    already: list[str] = []
    for parent_id in parent_ids:
        try:
            parent_path = find_artifact_path(parent_id, kb_root=kb_root)
        except ArtifactNotFoundError as exc:
            raise ArtifactCreateError(
                f"parent artifact {parent_id} not found under {kb_root}"
            ) from exc
        if add_backlink(parent_path, child_id):
            patched.append(_relative_posix(parent_path, repo_root=repo_root))
        else:
            already.append(_relative_posix(parent_path, repo_root=repo_root))
    return patched, already


def _resolve_roots(repo_root: str | Path | None) -> tuple[Path, Path]:
    root = Path(repo_root).resolve() if repo_root else find_repo_root()
    return root, root / "kb"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def new_hypothesis(
    *,
    title: str,
    repo_root: str | Path | None = None,
    artifact_id: str | None = None,
    extra_links: list[str] | None = None,
) -> ArtifactCreateResult:
    """Create a new ``H###`` hypothesis artifact.

    Parameters
    ----------
    title
        Human-readable title. Used as both the H1 heading content and the
        filename slug.
    repo_root
        Consumer repo root. Defaults to :func:`find_repo_root`.
    artifact_id
        Force a specific id (``H007``). Defaults to the smallest unused.
    extra_links
        Extra targets to include in the ``## Links`` block on top of the
        always-required ``ACTIVE`` / ``CHALLENGE`` (e.g. a prior hypothesis
        this one supersedes). No backlinks are patched for these.
    """
    if not title.strip():
        raise ArtifactCreateError("title is required")
    repo, kb = _resolve_roots(repo_root)
    aid = artifact_id or next_artifact_id("H", kb_root=kb)
    if _artifact_exists(aid, kb_root=kb):
        raise ArtifactCreateError(f"hypothesis {aid} already exists")

    extras = list(extra_links or [])
    link_targets = [*extras, *_ALWAYS_LINK]
    rendered = _render_template(
        _load_template("H", repo_root=repo),
        {
            "ARTIFACT_ID": aid,
            "TITLE": title,
            "DATE": _today_iso(),
            "LINKS_BLOCK": _render_links_block(link_targets),
        },
    )
    path = _write_artifact(
        kind="H",
        kb_root=kb,
        repo_root=repo,
        artifact_id=aid,
        slug=slugify(title),
        rendered=rendered,
    )
    # Hypotheses have no mandatory parents to backlink. Extra links are
    # deliberately *not* patched — the semantics of "this H supersedes H007"
    # are caller-defined; we don't want to silently graft bidirectional links.
    return ArtifactCreateResult(
        artifact_id=aid,
        path=_relative_posix(path, repo_root=repo),
    )


def new_experiment(
    *,
    title: str,
    hypothesis_id: str,
    repo_root: str | Path | None = None,
    artifact_id: str | None = None,
    extra_links: list[str] | None = None,
) -> ArtifactCreateResult:
    """Create a new ``E###`` experiment under an existing hypothesis.

    The parent ``H###`` must exist on disk — ``enforce_hef_chain`` would
    otherwise block the write at the hook layer. We surface the same failure
    here as an :class:`ArtifactCreateError` so callers get a clean error
    rather than a hook exit-code 2.
    """
    if not title.strip():
        raise ArtifactCreateError("title is required")
    if not re.fullmatch(r"H\d{3}", hypothesis_id):
        raise ArtifactCreateError(
            f"hypothesis_id must match H###, got {hypothesis_id!r}"
        )
    repo, kb = _resolve_roots(repo_root)
    if not _artifact_exists(hypothesis_id, kb_root=kb):
        raise ArtifactCreateError(
            f"hypothesis {hypothesis_id} does not exist under {kb}"
        )
    aid = artifact_id or next_artifact_id("E", kb_root=kb)
    if _artifact_exists(aid, kb_root=kb):
        raise ArtifactCreateError(f"experiment {aid} already exists")

    extras = list(extra_links or [])
    link_targets = [hypothesis_id, *extras, *_ALWAYS_LINK]
    rendered = _render_template(
        _load_template("E", repo_root=repo),
        {
            "ARTIFACT_ID": aid,
            "TITLE": title,
            "DATE": _today_iso(),
            "HYPOTHESIS_ID": hypothesis_id,
            "LINKS_BLOCK": _render_links_block(link_targets),
        },
    )
    path = _write_artifact(
        kind="E",
        kb_root=kb,
        repo_root=repo,
        artifact_id=aid,
        slug=slugify(title),
        rendered=rendered,
    )
    patched, already = _patch_parents(
        parent_ids=[hypothesis_id],
        child_id=aid,
        kb_root=kb,
        repo_root=repo,
    )
    return ArtifactCreateResult(
        artifact_id=aid,
        path=_relative_posix(path, repo_root=repo),
        backlinks_patched=patched,
        backlinks_already_present=already,
    )


def new_finding(
    *,
    title: str,
    hypothesis_id: str,
    experiment_id: str,
    impact: str = "MEDIUM",
    repo_root: str | Path | None = None,
    artifact_id: str | None = None,
    extra_links: list[str] | None = None,
) -> ArtifactCreateResult:
    """Create a new ``F###`` finding citing one hypothesis + one experiment.

    Patches both parents' ``## Links`` sections so ``kb_validate``'s
    bidirectional link check passes without further editing. Additional run
    citations (``supporting_runs:``) are deliberately *not* written here —
    the close-run / close-batch slash commands populate those once the user
    has the specific job id or batch selector.
    """
    if not title.strip():
        raise ArtifactCreateError("title is required")
    if not re.fullmatch(r"H\d{3}", hypothesis_id):
        raise ArtifactCreateError(
            f"hypothesis_id must match H###, got {hypothesis_id!r}"
        )
    if not re.fullmatch(r"E\d{3}", experiment_id):
        raise ArtifactCreateError(
            f"experiment_id must match E###, got {experiment_id!r}"
        )
    repo, kb = _resolve_roots(repo_root)
    if not _artifact_exists(hypothesis_id, kb_root=kb):
        raise ArtifactCreateError(
            f"hypothesis {hypothesis_id} does not exist under {kb}"
        )
    if not _artifact_exists(experiment_id, kb_root=kb):
        raise ArtifactCreateError(
            f"experiment {experiment_id} does not exist under {kb}"
        )

    aid = artifact_id or next_artifact_id("F", kb_root=kb)
    if _artifact_exists(aid, kb_root=kb):
        raise ArtifactCreateError(f"finding {aid} already exists")

    extras = list(extra_links or [])
    link_targets = [hypothesis_id, experiment_id, *extras, *_ALWAYS_LINK]
    rendered = _render_template(
        _load_template("F", repo_root=repo),
        {
            "ARTIFACT_ID": aid,
            "TITLE": title,
            "DATE": _today_iso(),
            "HYPOTHESIS_ID": hypothesis_id,
            "EXPERIMENT_ID": experiment_id,
            "IMPACT": impact,
            "LINKS_BLOCK": _render_links_block(link_targets),
        },
    )
    path = _write_artifact(
        kind="F",
        kb_root=kb,
        repo_root=repo,
        artifact_id=aid,
        slug=slugify(title),
        rendered=rendered,
    )
    patched, already = _patch_parents(
        parent_ids=[hypothesis_id, experiment_id],
        child_id=aid,
        kb_root=kb,
        repo_root=repo,
    )
    return ArtifactCreateResult(
        artifact_id=aid,
        path=_relative_posix(path, repo_root=repo),
        backlinks_patched=patched,
        backlinks_already_present=already,
    )


__all__ = [
    "ArtifactCreateError",
    "ArtifactCreateResult",
    "new_experiment",
    "new_finding",
    "new_hypothesis",
    "next_artifact_id",
    "slugify",
]
