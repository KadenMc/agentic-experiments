"""Stop hook: run a full KB validation before Claude finishes the turn.

Runs :func:`aexp.kb_validate.validate_kb` over the full ``kb/`` tree of the
checkout the session is working in: the one holding the session's current
directory when it belongs to the session's own repository, a worktree
included, and otherwise the project the session started in. See
:mod:`aexp.hooks._project`. Exit code ``2`` blocks the turn from closing
cleanly when structural validation fails.
"""
from __future__ import annotations

import sys

from aexp.hooks._project import read_event, read_stdin, session_checkout
from aexp.hooks._stdio import use_utf8
from aexp.kb_validate import format_text, validate_kb


def main() -> int:
    use_utf8()
    event = read_event(read_stdin())
    kb_root = session_checkout(event) / "kb"

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
