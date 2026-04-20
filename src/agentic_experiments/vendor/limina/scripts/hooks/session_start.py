#!/usr/bin/env python3
"""SessionStart hook: inject the small runtime state the agent needs now.

Prints ``kb/mission/CHALLENGE.md`` and ``kb/ACTIVE.md`` contents (or a
warning if they are missing), then invokes the telemetry script for
consent + session-open emission. Port of ``session_start.sh``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_SCRIPT = PROJECT_ROOT / "scripts" / "telemetry.py"


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


def _telemetry_call(args: list[str]) -> None:
    """Invoke telemetry.py with given args; silence all errors."""
    if not TELEMETRY_SCRIPT.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(TELEMETRY_SCRIPT), *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def main() -> int:
    emit_file("kb/mission/CHALLENGE.md", PROJECT_ROOT / "kb" / "mission" / "CHALLENGE.md")
    emit_file("kb/ACTIVE.md", PROJECT_ROOT / "kb" / "ACTIVE.md")

    _telemetry_call(
        [
            "ensure-consent",
            "--runtime-family",
            "claude",
            "--source",
            "claude_session_start",
        ]
    )
    _telemetry_call(
        [
            "session-open",
            "--runtime-family",
            "claude",
            "--emitter",
            "claude_session_start",
            "--flush",
        ]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
