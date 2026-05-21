"""KB structural validator — frontmatter, aliases, wikilinks, H->E->F chain.

Ported from the vendored ``kb_validate.py`` into the package. Callable
in-process via :func:`validate_kb`; :func:`main` preserves a ``python -m
aexp.kb_validate`` CLI for parity with the old script invocation.

The upstream validator's optional telemetry hooks are intentionally dropped
— ``aexp`` does not emit to any external telemetry sink.
"""

from __future__ import annotations

import argparse
import ast
import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import frontmatter as _fm

    HAS_FRONTMATTER = True
except ImportError:
    HAS_FRONTMATTER = False


META_RE = re.compile(r"^>\s+\*\*(.+?)\*\*:\s*(.+?)\s*$")
FRONTMATTER_BLOCK_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
ARTIFACT_ID_RE = re.compile(r"^(?:CR|SR|H|E|F|L|T)\d{3}$")


@dataclass(frozen=True)
class ArtifactSpec:
    prefix: str
    directory: Path
    required_fields: tuple[str, ...]


@dataclass
class NoteRecord:
    name: str
    kind: str
    path: Path
    text: str
    metadata: dict[str, str]
    aliases: set[str]
    has_links_section: bool
    links: set[str]
    spec: ArtifactSpec | None = None


CORE_ARTIFACTS: dict[str, ArtifactSpec] = {
    "H": ArtifactSpec("H", Path("research/hypotheses"), ("Status", "Created")),
    "E": ArtifactSpec("E", Path("research/experiments"), ("Status", "Hypothesis", "Created")),
    "F": ArtifactSpec("F", Path("research/findings"), ("Hypothesis", "Experiment", "Impact", "Created")),
    "L": ArtifactSpec("L", Path("research/literature"), ("Type", "Date reviewed", "Relevance")),
    "CR": ArtifactSpec("CR", Path("reports"), ("Target", "Requested by", "Reviewer", "Date")),
    "SR": ArtifactSpec("SR", Path("reports"), ("Scope", "Challenge Review", "Date")),
    "T": ArtifactSpec("T", Path("research/threads"), ("Status", "Created")),
}

SPECIAL_NOTES = {
    "ACTIVE": {
        "path": Path("ACTIVE.md"),
        "required_headings": ("## Current Objective", "## Next Step", "## Blocker", "## Links"),
    },
    "CHALLENGE": {
        "path": Path("mission/CHALLENGE.md"),
        "required_headings": ("## Objective", "## Context", "## Success Criteria", "## Constraints", "## Links"),
    },
    "DASHBOARD": {
        "path": Path("DASHBOARD.md"),
        "required_headings": ("## Entry Points", "## Links"),
    },
}

_FRONTMATTER_TO_META = {
    "id": "ID",
    "status": "Status",
    "hypothesis": "Hypothesis",
    "experiment": "Experiment",
    "thread": "Thread",
    "impact": "Impact",
    "source_type": "Type",
    "relevance": "Relevance",
    "target": "Target",
    "target_id": "Target ID",
    "requested_by": "Requested by",
    "reviewer": "Reviewer",
    "scope": "Scope",
    "challenge_review": "Challenge Review",
    "created": "Created",
    "date_reviewed": "Date reviewed",
    "last_updated": "Last updated",
}


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []

    def add(self, check: str, message: str, path: Path | None = None) -> None:
        error = {"check": check, "message": message}
        if path is not None:
            error["path"] = str(path)
        self.errors.append(error)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "errors": self.errors,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the kb/ research graph.")
    parser.add_argument("--kb-root", default="./kb", help="Path to kb/ (default: ./kb)")
    parser.add_argument("--check-file", default=None, help="Validate one file in isolation")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--quiet", action="store_true", help="Suppress output when validation passes")
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def safe_relative_to(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def normalize_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(strip_quotes(str(item)) for item in value if str(item).strip())
    return strip_quotes(str(value)).strip()


def parse_frontmatter_values(text: str) -> dict[str, object]:
    if HAS_FRONTMATTER:
        try:
            post = _fm.loads(text)
            return dict(post.metadata)
        except Exception:
            pass

    match = FRONTMATTER_BLOCK_RE.match(text)
    if not match:
        return {}

    parsed: dict[str, object] = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        field_match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not field_match:
            continue
        key, raw_value = field_match.groups()
        value = raw_value.strip()
        if not value:
            parsed[key] = ""
            continue
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed[key] = ast.literal_eval(value)
                continue
            except Exception:
                pass
        parsed[key] = strip_quotes(value)
    return parsed


def extract_aliases(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {strip_quotes(str(item)).strip() for item in value if str(item).strip()}
    raw = strip_quotes(str(value)).strip()
    if not raw:
        return set()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = ast.literal_eval(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            return {strip_quotes(str(item)).strip() for item in parsed if str(item).strip()}
    return {raw}


def parse_blockquote_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        match = META_RE.match(line.strip())
        if match:
            metadata[match.group(1).strip()] = match.group(2).strip()
    return metadata


def parse_note_metadata(text: str) -> tuple[dict[str, str], set[str]]:
    metadata = parse_blockquote_metadata(text)
    frontmatter = parse_frontmatter_values(text)
    aliases = extract_aliases(frontmatter.get("aliases"))

    for key, value in frontmatter.items():
        meta_key = _FRONTMATTER_TO_META.get(str(key))
        if meta_key is None:
            continue
        normalized = normalize_value(value)
        if normalized:
            metadata[meta_key] = normalized

    return metadata, aliases


def extract_links_section(text: str) -> tuple[bool, set[str]]:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "## Links":
            start = index + 1
            break
    if start is None:
        return False, set()

    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break

    section_text = "\n".join(lines[start:end])
    links = {match.group(1).strip() for match in WIKILINK_RE.finditer(section_text)}
    return True, links


def normalize_ref(value: str) -> str:
    raw = strip_quotes(str(value).strip())
    if raw in {"", "N/A", "None", "null"}:
        return ""
    match = WIKILINK_RE.fullmatch(raw)
    if match:
        return match.group(1).strip()
    return raw


def build_note_record(name: str, kind: str, path: Path, spec: ArtifactSpec | None = None) -> NoteRecord:
    text = read_text(path)
    metadata, aliases = parse_note_metadata(text)
    has_links_section, links = extract_links_section(text)
    return NoteRecord(
        name=name,
        kind=kind,
        path=path,
        text=text,
        metadata=metadata,
        aliases=aliases,
        has_links_section=has_links_section,
        links=links,
        spec=spec,
    )


def collect_special_notes(kb_root: Path) -> dict[str, NoteRecord]:
    notes: dict[str, NoteRecord] = {}
    for name, config in SPECIAL_NOTES.items():
        path = kb_root / config["path"]
        if path.exists():
            notes[name] = build_note_record(name, name, path)
    return notes


def collect_lessons(kb_root: Path) -> dict[str, NoteRecord]:
    lessons: dict[str, NoteRecord] = {}
    lesson_root = kb_root / "lessons"
    if not lesson_root.exists():
        return lessons

    for path in sorted(lesson_root.glob("*.md")):
        if path.name.startswith("."):
            continue
        lessons[path.stem] = build_note_record(path.stem, "LESSON", path)
    return lessons


def collect_artifacts(kb_root: Path, result: ValidationResult) -> dict[str, NoteRecord]:
    artifacts: dict[str, NoteRecord] = {}
    for spec in CORE_ARTIFACTS.values():
        directory = kb_root / spec.directory
        if not directory.exists():
            continue

        expected_re = re.compile(rf"^{spec.prefix}(\d{{3}})-.+\.md$")
        for path in sorted(directory.glob("*.md")):
            if path.name.startswith("."):
                continue
            if not expected_re.match(path.name):
                if spec.directory != Path("reports"):
                    result.add(
                        "filename",
                        f"{path.name} does not match the expected {spec.prefix}NNN-slug.md format.",
                        path,
                    )
                continue

            artifact_id = path.name.split("-", 1)[0]
            if artifact_id in artifacts:
                result.add("duplicate_id", f"Duplicate artifact ID: {artifact_id}", path)
                continue
            artifacts[artifact_id] = build_note_record(artifact_id, spec.prefix, path, spec)
    return artifacts


def register_note_name(
    name: str,
    note: NoteRecord,
    note_index: dict[str, NoteRecord],
    result: ValidationResult,
) -> None:
    key = name.strip()
    if not key:
        return
    existing = note_index.get(key)
    if existing is not None and existing.path != note.path:
        result.add(
            "alias",
            f"Link target '{key}' resolves to multiple notes: {existing.path.name} and {note.path.name}.",
            note.path,
        )
        return
    note_index[key] = note


def build_note_index(
    special_notes: dict[str, NoteRecord],
    artifacts: dict[str, NoteRecord],
    lessons: dict[str, NoteRecord],
    result: ValidationResult,
) -> dict[str, NoteRecord]:
    note_index: dict[str, NoteRecord] = {}
    for note in [*special_notes.values(), *artifacts.values(), *lessons.values()]:
        register_note_name(note.path.stem, note, note_index, result)
        register_note_name(note.name, note, note_index, result)
        for alias in sorted(note.aliases):
            register_note_name(alias, note, note_index, result)
    return note_index


def validate_special_note(note: NoteRecord, result: ValidationResult) -> None:
    config = SPECIAL_NOTES[note.name]
    for heading in config["required_headings"]:
        if heading not in note.text:
            result.add("structure", f"{note.path.name} is missing heading: {heading}", note.path)

    if note.name not in note.aliases:
        result.add("aliases", f"{note.path.name} must alias '{note.name}' in frontmatter.", note.path)


def validate_required_fields(note: NoteRecord, result: ValidationResult) -> None:
    if note.spec is None:
        return
    for field in note.spec.required_fields:
        if not note.metadata.get(field):
            result.add("metadata", f"{note.path.name} is missing required metadata field '{field}'.", note.path)


def validate_artifact_identity(note: NoteRecord, result: ValidationResult) -> None:
    declared_id = normalize_ref(note.metadata.get("ID", ""))
    if declared_id and declared_id != note.name:
        result.add("metadata", f"{note.path.name} frontmatter id is '{declared_id}', expected '{note.name}'.", note.path)

    if note.name not in note.aliases:
        result.add("aliases", f"{note.path.name} must alias '{note.name}' in frontmatter.", note.path)


def validate_ref(
    note: NoteRecord,
    field: str,
    expected_prefix: str,
    artifacts: dict[str, NoteRecord],
    result: ValidationResult,
) -> str:
    ref = normalize_ref(note.metadata.get(field, ""))
    if not ref:
        return ""
    if not ref.startswith(expected_prefix):
        result.add("reference", f"{field} must reference a {expected_prefix} artifact, got '{ref}'.", note.path)
        return ref
    if ref not in artifacts:
        result.add("reference", f"{note.path.name} references missing artifact {ref}.", note.path)
    return ref


_JSON_PRIMITIVE_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


def _is_json_serializable_value(value: object) -> bool:
    """Recursive: primitives, or lists/dicts of them. Rejects tuples / sets /
    arbitrary objects — the validator is only attesting shape, not rigor."""
    if isinstance(value, _JSON_PRIMITIVE_TYPES):
        return True
    if isinstance(value, list):
        return all(_is_json_serializable_value(v) for v in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _is_json_serializable_value(v)
            for k, v in value.items()
        )
    return False


def validate_conditions_block(note: NoteRecord, result: ValidationResult) -> None:
    """Check that an experiment's ``conditions:`` frontmatter block is a
    dict of dicts of JSON primitives.

    Applies only to ``E###`` artifacts with a ``conditions:`` field. Absent
    field → no-op (backward compatible). Malformed field → ``conditions_schema``
    issue per offending entry.

    The validator here re-parses the raw frontmatter because ``NoteRecord``
    only carries mapped metadata keys (via ``_FRONTMATTER_TO_META``) — the
    ``conditions`` field isn't mapped and would otherwise be invisible.
    """
    raw_frontmatter = parse_frontmatter_values(note.text)
    conditions = raw_frontmatter.get("conditions")
    if conditions is None or conditions == "" or conditions == {}:
        return

    if not isinstance(conditions, dict):
        result.add(
            "conditions_schema",
            f"{note.path.name} 'conditions' must be a mapping of "
            f"<name>: <config-dict>, got {type(conditions).__name__}.",
            note.path,
        )
        return

    for cond_name, cond_block in conditions.items():
        if not isinstance(cond_name, str):
            result.add(
                "conditions_schema",
                f"{note.path.name} condition name {cond_name!r} must be a string.",
                note.path,
            )
            continue
        if not isinstance(cond_block, dict):
            result.add(
                "conditions_schema",
                f"{note.path.name} condition '{cond_name}' must be a mapping "
                f"of config keys to JSON-serializable values, got "
                f"{type(cond_block).__name__}.",
                note.path,
            )
            continue
        for key, value in cond_block.items():
            if not isinstance(key, str):
                result.add(
                    "conditions_schema",
                    f"{note.path.name} condition '{cond_name}' has non-string "
                    f"key {key!r}.",
                    note.path,
                )
                continue
            if not _is_json_serializable_value(value):
                result.add(
                    "conditions_schema",
                    f"{note.path.name} condition '{cond_name}.{key}' value is "
                    f"not JSON-serializable: got {type(value).__name__}.",
                    note.path,
                )


_TEMPLATE_FILENAMES_FOR_HEADER_CHECK: dict[str, str] = {
    "H": "hypothesis.md",
    "E": "experiment.md",
    "F": "finding.md",
    "T": "thread.md",
}
_VENDOR_TEMPLATES_DIR = (
    Path(__file__).resolve().parent / "vendor" / "limina" / "templates"
)


@functools.cache
def _required_headers_for_kind(kind: str) -> tuple[str, ...]:
    """Extract ordered ``## ``-level headers from the vendored template for ``kind``.

    Returns the headers an artifact of this kind is expected to contain.
    Excludes ``## Links`` (already comprehensively validated by
    :func:`validate_links`) so we don't double-report. Returns an empty
    tuple for kinds without a tracked template (e.g. ``L``, ``CR``,
    ``SR`` — those aren't enforced yet).
    """
    filename = _TEMPLATE_FILENAMES_FOR_HEADER_CHECK.get(kind)
    if filename is None:
        return ()
    template_path = _VENDOR_TEMPLATES_DIR / filename
    if not template_path.is_file():
        return ()
    headers: list[str] = []
    for raw_line in template_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            header = line[3:].strip()
            if header == "Links":
                # Covered by validate_links — avoid double-reporting.
                continue
            headers.append(header)
    return tuple(headers)


_H2_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def validate_required_headers(note: NoteRecord, result: ValidationResult) -> None:
    """Check that an artifact contains every top-level ``## `` header
    declared by its kind's shipped template.

    Per Kaden's design directive: *"we need to stick to the templates
    precisely."* Authors who delete a template section under pressure
    (deciding it "doesn't apply") are caught here. Extra headers beyond
    the template are allowed — artifacts can extend, just not contract.

    Currently enforced for H / E / F. L / CR / SR are not yet checked.
    """
    required = _required_headers_for_kind(note.kind)
    if not required:
        return
    found = {m.group(1).strip() for m in _H2_HEADER_RE.finditer(note.text)}
    for header in required:
        if header not in found:
            result.add(
                "missing_template_header",
                f"{note.path.name} is missing required template header "
                f"'## {header}'. Templates are contracts, not suggestions — "
                "fill the section (with a placeholder if there's nothing to "
                "say yet) or update the shipped template.",
                note.path,
            )


def validate_artifact(note: NoteRecord, artifacts: dict[str, NoteRecord], note_index: dict[str, NoteRecord], result: ValidationResult) -> None:
    validate_required_fields(note, result)
    validate_artifact_identity(note, result)
    validate_required_headers(note, result)

    if note.kind == "H":
        # Optional `Thread` field — when set, must reference an existing T###.
        validate_ref(note, "Thread", "T", artifacts, result)
    elif note.kind == "E":
        validate_ref(note, "Hypothesis", "H", artifacts, result)
        validate_conditions_block(note, result)
    elif note.kind == "F":
        hypothesis_id = validate_ref(note, "Hypothesis", "H", artifacts, result)
        experiment_id = validate_ref(note, "Experiment", "E", artifacts, result)
        if experiment_id in artifacts:
            experiment_hypothesis = normalize_ref(artifacts[experiment_id].metadata.get("Hypothesis", ""))
            if experiment_hypothesis and hypothesis_id and experiment_hypothesis != hypothesis_id:
                result.add(
                    "reference",
                    f"{note.path.name} links {hypothesis_id}, but {experiment_id} links {experiment_hypothesis}.",
                    note.path,
                )
    elif note.kind == "T":
        # Threads are not in the H→E→F enforcement chain — they're parent
        # context for hypotheses, validated structurally only.
        pass
    elif note.kind == "CR":
        target_id = normalize_ref(note.metadata.get("Target ID", ""))
        if target_id and target_id not in note_index:
            result.add("reference", f"{note.path.name} references missing target note {target_id}.", note.path)
    elif note.kind == "SR":
        validate_ref(note, "Challenge Review", "CR", artifacts, result)


def note_links_to_target(note: NoteRecord, target: str, note_index: dict[str, NoteRecord]) -> bool:
    resolved_target = note_index.get(target)
    if resolved_target is None:
        return False
    for raw_link in note.links:
        linked_note = note_index.get(raw_link)
        if linked_note is not None and linked_note.path == resolved_target.path:
            return True
    return False


def required_links_for(note: NoteRecord) -> set[str]:
    if note.kind == "ACTIVE":
        return {"CHALLENGE"}
    if note.kind == "CHALLENGE":
        return {"ACTIVE", "DASHBOARD"}
    if note.kind == "DASHBOARD":
        return {"ACTIVE", "CHALLENGE"}

    required = {"ACTIVE", "CHALLENGE"}
    if note.kind == "H":
        thread_id = normalize_ref(note.metadata.get("Thread", ""))
        if thread_id:
            required.add(thread_id)
    elif note.kind == "E":
        hypothesis_id = normalize_ref(note.metadata.get("Hypothesis", ""))
        if hypothesis_id:
            required.add(hypothesis_id)
    elif note.kind == "F":
        hypothesis_id = normalize_ref(note.metadata.get("Hypothesis", ""))
        experiment_id = normalize_ref(note.metadata.get("Experiment", ""))
        if hypothesis_id:
            required.add(hypothesis_id)
        if experiment_id:
            required.add(experiment_id)
    elif note.kind == "CR":
        target_id = normalize_ref(note.metadata.get("Target ID", ""))
        if target_id:
            required.add(target_id)
    elif note.kind == "SR":
        challenge_review_id = normalize_ref(note.metadata.get("Challenge Review", ""))
        if challenge_review_id:
            required.add(challenge_review_id)
    return required


def validate_links(note: NoteRecord, note_index: dict[str, NoteRecord], result: ValidationResult) -> None:
    if not note.has_links_section:
        result.add("links", f"{note.path.name} must contain a ## Links section.", note.path)
        return
    if not note.links:
        result.add("links", f"{note.path.name} ## Links section must include at least one wikilink.", note.path)
        return

    for target in sorted(note.links):
        if target not in note_index:
            result.add("links", f"{note.path.name} links missing note [[{target}]].", note.path)

    for target in sorted(required_links_for(note)):
        if target and not note_links_to_target(note, target, note_index):
            result.add("links", f"{note.path.name} must link to [[{target}]] in ## Links.", note.path)


def validate_backlink(parent_ref: str, child_note: NoteRecord, note_index: dict[str, NoteRecord], result: ValidationResult) -> None:
    parent_note = note_index.get(parent_ref)
    if parent_note is None:
        return
    if not note_links_to_target(parent_note, child_note.name, note_index):
        result.add("backlink", f"{parent_note.path.name} must link back to [[{child_note.name}]].", parent_note.path)


def validate_backlinks(note: NoteRecord, note_index: dict[str, NoteRecord], result: ValidationResult) -> None:
    if note.kind == "H":
        thread_id = normalize_ref(note.metadata.get("Thread", ""))
        if thread_id:
            validate_backlink(thread_id, note, note_index, result)
    elif note.kind == "E":
        hypothesis_id = normalize_ref(note.metadata.get("Hypothesis", ""))
        if hypothesis_id:
            validate_backlink(hypothesis_id, note, note_index, result)
    elif note.kind == "F":
        hypothesis_id = normalize_ref(note.metadata.get("Hypothesis", ""))
        experiment_id = normalize_ref(note.metadata.get("Experiment", ""))
        if hypothesis_id:
            validate_backlink(hypothesis_id, note, note_index, result)
        if experiment_id:
            validate_backlink(experiment_id, note, note_index, result)
    elif note.kind == "CR":
        target_id = normalize_ref(note.metadata.get("Target ID", ""))
        if target_id:
            validate_backlink(target_id, note, note_index, result)
    elif note.kind == "SR":
        challenge_review_id = normalize_ref(note.metadata.get("Challenge Review", ""))
        if challenge_review_id:
            validate_backlink(challenge_review_id, note, note_index, result)


def validate_note(
    note: NoteRecord,
    artifacts: dict[str, NoteRecord],
    note_index: dict[str, NoteRecord],
    result: ValidationResult,
) -> None:
    if note.kind in SPECIAL_NOTES:
        validate_special_note(note, result)
    elif note.spec is not None:
        validate_artifact(note, artifacts, note_index, result)
    else:
        result.add("scope", f"{note.path.name} is not part of the validated research graph.", note.path)
        return

    validate_links(note, note_index, result)
    validate_backlinks(note, note_index, result)


def find_note_for_path(
    path: Path,
    kb_root: Path,
    special_notes: dict[str, NoteRecord],
    artifacts: dict[str, NoteRecord],
    lessons: dict[str, NoteRecord],
) -> NoteRecord | None:
    rel_path = safe_relative_to(path, kb_root)
    if rel_path is None:
        return None

    if rel_path == SPECIAL_NOTES["ACTIVE"]["path"]:
        return special_notes.get("ACTIVE")
    if rel_path == SPECIAL_NOTES["CHALLENGE"]["path"]:
        return special_notes.get("CHALLENGE")
    if rel_path == SPECIAL_NOTES["DASHBOARD"]["path"]:
        return special_notes.get("DASHBOARD")

    artifact_id = path.name.split("-", 1)[0]
    if artifact_id in artifacts:
        return artifacts[artifact_id]

    if rel_path.parent == Path("lessons"):
        return lessons.get(path.stem)

    return None


def format_text(result: ValidationResult) -> str:
    lines = [f"Validation failed with {len(result.errors)} error(s):"]
    for error in result.errors:
        prefix = f"- [{error['check']}]"
        if "path" in error:
            lines.append(f"{prefix} {error['path']}: {error['message']}")
        else:
            lines.append(f"{prefix} {error['message']}")
    return "\n".join(lines)


def validate_kb(
    kb_root: Path | str,
    *,
    check_file: Path | str | None = None,
) -> ValidationResult:
    """Validate a ``kb/`` tree (or a single file inside it).

    Pure in-process entry point — no I/O beyond reading the KB tree, no CLI
    side effects. Hooks, ``aexp validate``, and tests all call this directly.

    Parameters
    ----------
    kb_root
        Path to the ``kb/`` directory to validate.
    check_file
        If provided, validate only this single file (must live under
        ``kb_root`` and be one of the recognised artifact kinds).

    Returns
    -------
    ValidationResult
        Collected errors. ``result.ok`` is ``True`` iff no errors were found.
    """
    kb_root = Path(kb_root).resolve()
    result = ValidationResult()

    if not kb_root.exists():
        result.add("filesystem", f"kb root not found: {kb_root}")
        return result

    active_path = kb_root / SPECIAL_NOTES["ACTIVE"]["path"]
    challenge_path = kb_root / SPECIAL_NOTES["CHALLENGE"]["path"]
    if not active_path.exists():
        result.add("filesystem", "Missing kb/ACTIVE.md.", active_path)
    if not challenge_path.exists():
        result.add("filesystem", "Missing kb/mission/CHALLENGE.md.", challenge_path)

    special_notes = collect_special_notes(kb_root)
    lessons = collect_lessons(kb_root)
    artifacts = collect_artifacts(kb_root, result)
    note_index = build_note_index(special_notes, artifacts, lessons, result)

    if check_file is not None:
        target_path = Path(check_file).resolve()
        if not target_path.exists():
            result.add("filesystem", "File does not exist.", target_path)
        else:
            note = find_note_for_path(target_path, kb_root, special_notes, artifacts, lessons)
            if note is None:
                result.add(
                    "scope",
                    "File is not part of the validated research graph. Only ACTIVE.md, mission/CHALLENGE.md, DASHBOARD.md, and H/E/F/L/CR/SR artifacts are validated.",
                    target_path,
                )
            else:
                validate_note(note, artifacts, note_index, result)
    else:
        for required_name in ("ACTIVE", "CHALLENGE"):
            if required_name in special_notes:
                validate_note(special_notes[required_name], artifacts, note_index, result)
        if "DASHBOARD" in special_notes:
            validate_note(special_notes["DASHBOARD"], artifacts, note_index, result)
        for artifact_id in sorted(artifacts):
            validate_note(artifacts[artifact_id], artifacts, note_index, result)

    return result


def main() -> int:
    args = parse_args()
    result = validate_kb(args.kb_root, check_file=args.check_file)

    if args.format == "json":
        print(json.dumps(result.as_json(), indent=2))
    elif not result.ok or not args.quiet:
        print("KB validation passed." if result.ok else format_text(result))

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
