"""Stop hook: run a full KB validation before Claude finishes the turn.

Runs :func:`aexp.kb_validate.validate_kb` over the full ``kb/`` tree of the
current repo (derived from ``cwd``). Exit code ``2`` blocks the turn from
closing cleanly when structural validation fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

from aexp.kb_validate import format_text, validate_kb


def main() -> int:
    # Drain stdin (safe if empty; matches the `exec 0</dev/null` redirect in
    # the upstream shell version — no hook input is needed for stop-hook work).
    try:
        _ = sys.stdin.read()
    except Exception:
        pass

    repo_root = Path.cwd()
    kb_root = repo_root / "kb"

    if not kb_root.is_dir():
        return 0

    try:
        result = validate_kb(kb_root)
    except Exception as exc:  # validator blew up -> do not block
        print(f"(stop_validate: validator failed: {exc})", file=sys.stderr)
        return 0

    if not result.ok:
        print("BLOCKED: kb validation failed before stop.", file=sys.stderr)
        print(format_text(result), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
