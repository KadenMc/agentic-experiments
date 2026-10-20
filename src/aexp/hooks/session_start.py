"""SessionStart hook: inject ``kb/ACTIVE.md`` + ``kb/mission/CHALLENGE.md``.

The files come from the checkout the session is working in, resolved by
:func:`aexp.hooks._project.session_checkout`, which in a desktop-app worktree
session is the worktree rather than the main checkout. We read the two
reference files (if present) and print them to stdout wrapped in
``=== <label> ===`` headers -- Claude Code surfaces stdout from SessionStart
hooks as additional context for the session.
"""
from __future__ import annotations

import sys
from pathlib import Path

from aexp.hooks._project import read_event, read_stdin, session_checkout
from aexp.hooks._stdio import use_utf8


def emit_file(label: str, path: Path) -> None:
    """Print ``=== label ===`` followed by file contents.

    Silently no-ops when the file is absent -- consumer repos may legitimately
    defer authoring ``kb/mission/CHALLENGE.md`` until they have a mission
    statement worth writing down. ``aexp validate`` surfaces the same fact as
    a structured issue when it actually matters.
    """
    if not path.is_file():
        return
    print(f"=== {label} ===")
    try:
        print(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"(error reading file: {exc})")
    print()


def main() -> int:
    use_utf8()
    repo_root = session_checkout(read_event(read_stdin()))
    emit_file("kb/mission/CHALLENGE.md", repo_root / "kb" / "mission" / "CHALLENGE.md")
    emit_file("kb/ACTIVE.md", repo_root / "kb" / "ACTIVE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
