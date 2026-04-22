"""PostToolUse hook: validate kb/ writes immediately.

Runs :func:`aexp.kb_validate.validate_kb` in-process against the single file
the agent just wrote/edited. Exit code ``2`` surfaces the validation error
back to Claude Code so invalid KB edits do not go unnoticed.

Skipped paths:

- non-``.md`` files
- files outside ``kb/`` entirely
- files under ``kb/research/data/`` or ``kb/lessons/`` (carve-outs
  preserved from the upstream shell version)
- any path containing a dot-prefixed segment (hidden files)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from aexp.hooks._parse_hook_input import parse_hook_input
from aexp.kb_validate import format_text, validate_kb

CARVE_OUT_RE = re.compile(r"(^|/)kb/(research/data|lessons)/")
HIDDEN_SEGMENT_RE = re.compile(r"/\.[^/]+")


def _in_kb(normalized_fp: str) -> bool:
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

    repo_root = Path.cwd()
    kb_root = repo_root / "kb"

    abs_path = Path(fp) if Path(fp).is_file() else (repo_root / fp)

    try:
        result = validate_kb(kb_root, check_file=abs_path)
    except Exception as exc:  # validator blew up -> do not block
        print(f"(kb_write_guard: validator failed: {exc})", file=sys.stderr)
        return 0

    if not result.ok:
        print(f"BLOCKED: KB validation failed for {Path(fp).name}.", file=sys.stderr)
        print(format_text(result), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
