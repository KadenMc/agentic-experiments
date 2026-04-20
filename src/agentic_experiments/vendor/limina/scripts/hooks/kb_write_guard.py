#!/usr/bin/env python3
"""PostToolUse hook: validate kb/ writes immediately.

Exit code 2 surfaces the validation error back to Claude Code so invalid
kb edits do not go unnoticed. Port of ``kb_write_guard.sh``.

Skipped paths:
- non-``.md`` files
- files outside ``kb/`` entirely
- files under ``kb/research/data/`` or ``kb/lessons/`` (carve-outs preserved
  from the upstream shell version)
- any path containing a dot-prefixed segment (hidden files)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _parse_hook_input import parse_hook_input  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_ROOT = PROJECT_ROOT / "kb"
VALIDATOR = PROJECT_ROOT / "scripts" / "kb_validate.py"

CARVE_OUT_RE = re.compile(r"(^|/)kb/(research/data|lessons)/")
HIDDEN_SEGMENT_RE = re.compile(r"/\.[^/]+")


def _in_kb(normalized_fp: str) -> bool:
    """True if the path references something under a ``kb/`` directory."""
    return normalized_fp.startswith("kb/") or "/kb/" in normalized_fp


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    fp, _content = parse_hook_input(raw)
    if not fp:
        return 0

    normalized = fp.replace("\\", "/")

    if not _in_kb(normalized):
        return 0
    if not normalized.endswith(".md"):
        return 0
    if CARVE_OUT_RE.search(normalized):
        return 0
    if HIDDEN_SEGMENT_RE.search(normalized):
        return 0

    # Resolve to an absolute path the validator can read.
    abs_path = Path(fp) if Path(fp).is_file() else (PROJECT_ROOT / fp)

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--kb-root",
                str(KB_ROOT),
                "--check-file",
                str(abs_path),
                "--format",
                "text",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except Exception as exc:  # validator unavailable → do not block
        print(f"(kb_write_guard: validator invocation failed: {exc})", file=sys.stderr)
        return 0

    if result.returncode != 0:
        print(f"BLOCKED: KB validation failed for {Path(fp).name}.", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
