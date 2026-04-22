#!/usr/bin/env python3
"""PreToolUse hook: enforce the H -> E -> F chain.

Blocks experiment creation without a hypothesis reference, and finding
creation without an experiment reference. Exit code 2 signals a blocking
error to Claude Code (stderr is surfaced back to the agent).

Port of ``enforce_hef_chain.sh``. Behavior matches the shell version:

- File path patterns are ``kb/research/experiments/E{NNN}-*.md`` and
  ``kb/research/findings/F{NNN}-*.md``.
- The referenced field is extracted from YAML frontmatter OR from a
  blockquote line like ``> **Hypothesis**: H012``.
- If the reference exists but the target file is missing in
  ``kb/research/hypotheses/`` / ``kb/research/experiments/``, this is
  also a blocking error.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Use package-relative import for the parser.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _parse_hook_input import parse_hook_input  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_ROOT = PROJECT_ROOT / "kb"
TELEMETRY_SCRIPT = PROJECT_ROOT / "scripts" / "telemetry.py"

EXPERIMENT_PATH_RE = re.compile(r"kb/research/experiments/E\d{3}-.*\.md$")
FINDING_PATH_RE = re.compile(r"kb/research/findings/F\d{3}-.*\.md$")
BLOCKQUOTE_META_RE = re.compile(r"^>\s+\*\*(.+?)\*\*:\s*(.+?)\s*$")

try:
    import frontmatter as _frontmatter
    _HAS_FRONTMATTER = True
except ImportError:
    _frontmatter = None  # type: ignore[assignment]
    _HAS_FRONTMATTER = False


def _emit_block(code: str) -> None:
    """Emit a telemetry event for a blocked write; silence all errors."""
    if not TELEMETRY_SCRIPT.is_file():
        return
    try:
        subprocess.run(
            [
                sys.executable,
                str(TELEMETRY_SCRIPT),
                "emit",
                "limina_hef_blocked",
                "--runtime-family",
                "claude",
                "--emitter",
                "claude_hef_guard",
                "--property",
                f"result_code={code}",
                "--flush",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        pass


def extract_meta_ref(content: str, field: str, ref_pattern: str) -> str | None:
    """Extract a metadata reference from frontmatter or blockquote metadata.

    Parameters
    ----------
    content : str
        Full post-edit file content.
    field : str
        Metadata field name (case-insensitive), e.g. ``"hypothesis"``.
    ref_pattern : str
        Regex pattern matching the reference, e.g. ``r"H\\d{3}"``.

    Returns
    -------
    str | None
        The matched reference (e.g. ``"H012"``) or ``None``.
    """
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
    """Find a file in ``directory`` matching ``<artifact_id>-*.md``."""
    if not directory.is_dir():
        return None
    for p in directory.glob(f"{artifact_id}-*.md"):
        return p
    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    fp, content = parse_hook_input(raw)
    if not fp:
        return 0

    # Normalize to forward slashes for pattern matching (consistent with shell).
    normalized = fp.replace("\\", "/")

    # Experiment creation
    if EXPERIMENT_PATH_RE.search(normalized):
        hypo_id = extract_meta_ref(content, "hypothesis", r"H\d{3}")
        if not hypo_id:
            _emit_block("missing_hypothesis_ref")
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

        hypo_file = _find_existing_artifact(KB_ROOT / "research" / "hypotheses", hypo_id)
        if hypo_file is None:
            _emit_block("missing_hypothesis_file")
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

    # Finding creation
    if FINDING_PATH_RE.search(normalized):
        exp_id = extract_meta_ref(content, "experiment", r"E\d{3}")
        if not exp_id:
            _emit_block("missing_experiment_ref")
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

        exp_file = _find_existing_artifact(KB_ROOT / "research" / "experiments", exp_id)
        if exp_file is None:
            _emit_block("missing_experiment_file")
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
