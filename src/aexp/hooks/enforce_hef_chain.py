"""PreToolUse hook: enforce the H -> E -> F chain on kb/ writes.

Blocks:

- Creating an experiment without a ``hypothesis:`` reference (or a live
  hypothesis file to back it)
- Creating a finding without an ``experiment:`` reference (or a live
  experiment file to back it)

Exit code ``2`` tells Claude Code to block the tool use and surface the
stderr message to the agent. The referenced hypothesis or experiment is
looked up in the ``kb/`` of the checkout the edited file belongs to (see
:mod:`aexp.hooks._project`), and a file in another repository is left alone.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from aexp.hooks._parse_hook_input import parse_hook_input
from aexp.hooks._project import checkout_for, file_path_for, is_within, read_event
from aexp.hooks._stdio import use_utf8

HYPOTHESIS_PATH_RE = re.compile(r"kb/research/hypotheses/H\d{3}-.*\.md$")
EXPERIMENT_PATH_RE = re.compile(r"kb/research/experiments/E\d{3}-.*\.md$")
FINDING_PATH_RE = re.compile(r"kb/research/findings/F\d{3}-.*\.md$")
BLOCKQUOTE_META_RE = re.compile(r"^>\s+\*\*(.+?)\*\*:\s*(.+?)\s*$")

try:
    import frontmatter as _frontmatter
    _HAS_FRONTMATTER = True
except ImportError:
    _frontmatter = None  # type: ignore[assignment]
    _HAS_FRONTMATTER = False


def extract_meta_ref(content: str, field: str, ref_pattern: str) -> str | None:
    """Extract a metadata reference from frontmatter or blockquote metadata."""
    field_lower = field.lower()
    ref_re = re.compile(f"({ref_pattern})")

    if _HAS_FRONTMATTER:
        try:
            post = _frontmatter.loads(content)
            val = str(post.metadata.get(field_lower, ""))
            m = ref_re.search(val)
            if m:
                return m.group(1)
        except Exception:
            pass

    for line in content.splitlines():
        mm = BLOCKQUOTE_META_RE.match(line.strip())
        if mm and mm.group(1).strip().lower() == field_lower:
            m = ref_re.search(mm.group(2))
            if m:
                return m.group(1)

    return None


def _find_existing_artifact(directory: Path, artifact_id: str) -> Path | None:
    if not directory.is_dir():
        return None
    for p in directory.glob(f"{artifact_id}-*.md"):
        return p
    return None


def main() -> int:
    use_utf8()
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    fp, content = parse_hook_input(raw)
    if not fp:
        return 0

    event = read_event(raw)
    abs_path = file_path_for(fp, event)
    checkout = checkout_for(abs_path, event)
    if checkout is None or not is_within(abs_path, checkout / "kb"):
        return 0
    kb_root = checkout / "kb"

    # Normalize to forward slashes for pattern matching.
    normalized = fp.replace("\\", "/")

    if HYPOTHESIS_PATH_RE.search(normalized):
        # `thread:` field is OPTIONAL on hypotheses. Only enforce when set.
        thread_id = extract_meta_ref(content, "thread", r"T\d{3}")
        if thread_id:
            thread_file = _find_existing_artifact(
                kb_root / "research" / "threads", thread_id
            )
            if thread_file is None:
                print(
                    f"BLOCKED: Hypothesis references {thread_id}, but no thread "
                    "file found in kb/research/threads/.",
                    file=sys.stderr,
                )
                print(
                    "Create the thread first with `aexp new-thread`, or remove "
                    "the `thread:` field from this hypothesis.",
                    file=sys.stderr,
                )
                return 2

    if EXPERIMENT_PATH_RE.search(normalized):
        hypo_id = extract_meta_ref(content, "hypothesis", r"H\d{3}")
        if not hypo_id:
            print(
                "BLOCKED: Cannot create experiment without a hypothesis reference.",
                file=sys.stderr,
            )
            print(
                "Add 'hypothesis: \"H{NUM}\"' to the YAML frontmatter or "
                "'> **Hypothesis**: H{NUM}' to the metadata.",
                file=sys.stderr,
            )
            return 2

        hypo_file = _find_existing_artifact(kb_root / "research" / "hypotheses", hypo_id)
        if hypo_file is None:
            print(
                f"BLOCKED: Experiment references {hypo_id}, but no hypothesis file found "
                "in kb/research/hypotheses/.",
                file=sys.stderr,
            )
            print(
                "Create the hypothesis file first (Rule 3: NEVER run an experiment "
                "without a hypothesis file).",
                file=sys.stderr,
            )
            return 2

    if FINDING_PATH_RE.search(normalized):
        exp_id = extract_meta_ref(content, "experiment", r"E\d{3}")
        if not exp_id:
            print(
                "BLOCKED: Cannot create finding without an experiment reference.",
                file=sys.stderr,
            )
            print(
                "Add 'experiment: \"E{NUM}\"' to the YAML frontmatter or "
                "'> **Experiment**: E{NUM}' to the metadata.",
                file=sys.stderr,
            )
            return 2

        exp_file = _find_existing_artifact(kb_root / "research" / "experiments", exp_id)
        if exp_file is None:
            print(
                f"BLOCKED: Finding references {exp_id}, but no experiment file found "
                "in kb/research/experiments/.",
                file=sys.stderr,
            )
            print(
                "Create the experiment file first (Rule 4: NEVER create a finding "
                "without linking it to an experiment).",
                file=sys.stderr,
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
