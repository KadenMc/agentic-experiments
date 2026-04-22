"""SessionStart hook: inject ``kb/ACTIVE.md`` + ``kb/mission/CHALLENGE.md``.

Claude Code invokes this with ``cwd`` set to the consumer repo root. We read
the two reference files (if present) and print them to stdout wrapped in
``=== <label> ===`` headers — Claude Code surfaces stdout from SessionStart
hooks as additional context for the session.
"""
from __future__ import annotations

import sys
from pathlib import Path


def emit_file(label: str, path: Path) -> None:
    """Print ``=== label ===`` followed by file contents, or a warning."""
    if path.is_file():
        print(f"=== {label} ===")
        try:
            print(path.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"(error reading file: {exc})")
        print()
    else:
        print(f"=== WARNING: {label} not found ===")


def main() -> int:
    repo_root = Path.cwd()
    emit_file("kb/mission/CHALLENGE.md", repo_root / "kb" / "mission" / "CHALLENGE.md")
    emit_file("kb/ACTIVE.md", repo_root / "kb" / "ACTIVE.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
