"""Typed read wrappers over Limina ``kb/`` artifacts.

Reads parse the YAML frontmatter with ``python-frontmatter`` and return a
:class:`~aexp.schema.ArtifactRef`. Everything here is read-only;
artifact creation happens by writing from ``templates/`` (v1) or via the
planned ``aexp new-hypothesis`` / ``new-experiment`` / ``new-finding``
CLI verbs (v1.1).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import frontmatter

from aexp.schema import ArtifactKind, ArtifactRef

# ---------------------------------------------------------------------------
# Layout constants (mirror kb_validate.CORE_ARTIFACTS)
# ---------------------------------------------------------------------------

_KIND_DIRS: dict[ArtifactKind, Path] = {
    "H": Path("research") / "hypotheses",
    "E": Path("research") / "experiments",
    "F": Path("research") / "findings",
    "L": Path("research") / "literature",
    "CR": Path("reports"),
    "SR": Path("reports"),
    "T": Path("research") / "threads",
}

_ID_RE = re.compile(r"^(CR|SR|H|E|F|L|T)(\d{3})$")
_FILENAME_ID_RE = re.compile(r"^(CR|SR|H|E|F|L|T)(\d{3})-")
_H1_RE = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)


class ArtifactNotFoundError(FileNotFoundError):
    """No file matching an artifact id was found under the expected directory."""


class ArtifactReadError(RuntimeError):
    """An artifact file existed but could not be parsed (YAML error, missing id, etc.)."""


# ---------------------------------------------------------------------------
# ID / path helpers
# ---------------------------------------------------------------------------


def parse_artifact_id(artifact_id: str) -> tuple[ArtifactKind, int]:
    """Split an artifact id like ``"E018"`` into ``("E", 18)``.

    Raises ``ValueError`` if ``artifact_id`` does not match ``^(CR|SR|H|E|F|L|T)\\d{3}$``.
    """
    m = _ID_RE.match(artifact_id)
    if not m:
        raise ValueError(
            f"invalid artifact id {artifact_id!r}; expected e.g. 'H001', 'E018', 'F007'"
        )
    kind = m.group(1)
    number = int(m.group(2))
    return kind, number  # type: ignore[return-value]


def kind_dir(kind: ArtifactKind, kb_root: Path) -> Path:
    """Return the directory under ``kb_root`` that stores artifacts of ``kind``."""
    if kind not in _KIND_DIRS:
        raise ValueError(f"unknown artifact kind: {kind!r}")
    return kb_root / _KIND_DIRS[kind]


def find_artifact_path(artifact_id: str, *, kb_root: Path) -> Path:
    """Return the on-disk path of the artifact with id ``artifact_id``.

    Matches the filename pattern ``<id>-*.md`` that ``kb_new_artifact.py``
    produces.

    Raises
    ------
    ArtifactNotFoundError
        If no matching file exists.
    """
    kind, _ = parse_artifact_id(artifact_id)
    directory = kind_dir(kind, kb_root)
    if not directory.is_dir():
        raise ArtifactNotFoundError(
            f"expected directory {directory} to exist for kind {kind!r}"
        )
    matches = sorted(directory.glob(f"{artifact_id}-*.md"))
    if not matches:
        raise ArtifactNotFoundError(
            f"no artifact file matching {artifact_id}-*.md under {directory}"
        )
    if len(matches) > 1:
        # Limina requires one file per id; kb_validate flags duplicates. Be
        # conservative here and raise rather than pick silently.
        raise ArtifactReadError(
            f"multiple files match {artifact_id}-*.md under {directory}: "
            + ", ".join(p.name for p in matches)
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _extract_title(body: str, fallback: str) -> str:
    """Pull the first ``# <title>`` line from ``body``; fall back if absent."""
    m = _H1_RE.search(body)
    if not m:
        return fallback
    raw = m.group(1).strip()
    # Limina uses "{ID} — {Title}"; strip the leading id if present.
    for sep in (" — ", " - ", "— ", "- "):
        if sep in raw:
            _id, _, rest = raw.partition(sep)
            if _FILENAME_ID_RE.match(_id + "-"):
                return rest.strip() or raw
    return raw


def _load_artifact(path: Path, kb_root: Path) -> ArtifactRef:
    """Parse a Limina markdown artifact file into a typed reference."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactReadError(f"could not read {path}: {exc}") from exc

    try:
        post = frontmatter.loads(text)
    except Exception as exc:  # frontmatter can raise many exception types
        raise ArtifactReadError(f"could not parse frontmatter in {path}: {exc}") from exc

    metadata: dict[str, Any] = dict(post.metadata)
    body = post.content

    # Derive id: prefer frontmatter, fall back to filename parse.
    fm_id = str(metadata.get("id", "")).strip()
    artifact_id: str
    if _ID_RE.match(fm_id):
        artifact_id = fm_id
    else:
        fname_match = _FILENAME_ID_RE.match(path.name)
        if fname_match:
            artifact_id = f"{fname_match.group(1)}{fname_match.group(2)}"
        else:
            raise ArtifactReadError(
                f"cannot determine artifact id for {path}: frontmatter id={fm_id!r}, "
                "and filename does not start with <PREFIX><NUMBER>-"
            )

    kind, _ = parse_artifact_id(artifact_id)

    # Relative path (POSIX) for display.
    try:
        rel = path.resolve().relative_to(kb_root.resolve().parent).as_posix()
    except ValueError:
        rel = path.as_posix()

    title = _extract_title(body, fallback=artifact_id)

    return ArtifactRef(
        kind=kind,
        id=artifact_id,
        path=rel,
        title=title,
        metadata=metadata,
        body=body,
    )


def load_artifact(artifact_id: str, *, kb_root: Path) -> ArtifactRef:
    """Load any artifact by id (H/E/F/L/CR/SR)."""
    path = find_artifact_path(artifact_id, kb_root=kb_root)
    return _load_artifact(path, kb_root=kb_root)


def load_hypothesis(artifact_id: str, *, kb_root: Path) -> ArtifactRef:
    """Load a hypothesis artifact, asserting the kind for callers that care."""
    ref = load_artifact(artifact_id, kb_root=kb_root)
    if ref.kind != "H":
        raise ArtifactReadError(f"{artifact_id} is {ref.kind}, not H")
    return ref


def load_experiment(artifact_id: str, *, kb_root: Path) -> ArtifactRef:
    """Load an experiment artifact, asserting the kind."""
    ref = load_artifact(artifact_id, kb_root=kb_root)
    if ref.kind != "E":
        raise ArtifactReadError(f"{artifact_id} is {ref.kind}, not E")
    return ref


def load_finding(artifact_id: str, *, kb_root: Path) -> ArtifactRef:
    """Load a finding artifact, asserting the kind."""
    ref = load_artifact(artifact_id, kb_root=kb_root)
    if ref.kind != "F":
        raise ArtifactReadError(f"{artifact_id} is {ref.kind}, not F")
    return ref


def load_thread(artifact_id: str, *, kb_root: Path) -> ArtifactRef:
    """Load a thread artifact, asserting the kind."""
    ref = load_artifact(artifact_id, kb_root=kb_root)
    if ref.kind != "T":
        raise ArtifactReadError(f"{artifact_id} is {ref.kind}, not T")
    return ref


def list_kb_artifacts(
    kb_root: Path,
    *,
    kind: ArtifactKind | None = None,
) -> list[ArtifactRef]:
    """Walk ``kb_root`` and load every artifact of the given kind(s).

    Parameters
    ----------
    kb_root : Path
        Root of the ``kb/`` tree.
    kind : ArtifactKind | None
        If given, only return artifacts of that kind. Default: all kinds.
    """
    kinds: list[ArtifactKind] = [kind] if kind else list(_KIND_DIRS.keys())
    results: list[ArtifactRef] = []
    for k in kinds:
        directory = kind_dir(k, kb_root)
        if not directory.is_dir():
            continue
        # reports/ is shared between CR and SR; filter by filename prefix.
        for p in sorted(directory.glob("*.md")):
            if not _FILENAME_ID_RE.match(p.name):
                continue
            # When walking reports/ for both CR and SR, only pick files whose
            # filename id matches the current kind.
            if directory == kind_dir(k, kb_root) and not p.name.startswith(f"{k}"):
                continue
            try:
                results.append(_load_artifact(p, kb_root=kb_root))
            except ArtifactReadError:
                # Malformed artifact — skip silently; kb_validate surfaces it.
                continue
    return results


__all__ = [
    "ArtifactNotFoundError",
    "ArtifactReadError",
    "find_artifact_path",
    "kind_dir",
    "list_kb_artifacts",
    "load_artifact",
    "load_experiment",
    "load_finding",
    "load_hypothesis",
    "load_thread",
    "parse_artifact_id",
]
