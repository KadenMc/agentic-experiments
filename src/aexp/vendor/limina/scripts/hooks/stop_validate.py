#!/usr/bin/env python3
"""Stop hook: run a full kb validation before Claude finishes the turn.

Port of ``stop_validate.sh``. Reads stdin and discards it (matches the
``exec 0</dev/null`` pattern in the shell version — we do not need the
hook input here), then runs ``kb_validate.py`` over the full KB tree.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KB_ROOT = PROJECT_ROOT / "kb"
VALIDATOR = PROJECT_ROOT / "scripts" / "kb_validate.py"
TELEMETRY_SCRIPT = PROJECT_ROOT / "scripts" / "telemetry.py"


def _telemetry(args: list[str]) -> None:
    """Invoke telemetry.py with given args; silence all errors."""
    if not TELEMETRY_SCRIPT.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(TELEMETRY_SCRIPT), *args],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def main() -> int:
    # Drain stdin (safe if empty; matches the `exec 0</dev/null` redirect).
    try:
        _ = sys.stdin.read()
    except Exception:
        pass

    if not KB_ROOT.is_dir():
        return 0

    import os as _os

    env = {**_os.environ, "LIMINA_TELEMETRY_INTERNAL": "1"}
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--kb-root",
                str(KB_ROOT),
                "--format",
                "text",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
    except Exception as exc:
        print(f"(stop_validate: validator invocation failed: {exc})", file=sys.stderr)
        return 0

    if result.returncode != 0:
        _telemetry(
            [
                "emit",
                "limina_session_completed",
                "--runtime-family",
                "claude",
                "--emitter",
                "claude_stop",
                "--property",
                "result_code=validation_failed",
                "--flush",
            ]
        )
        print("BLOCKED: kb validation failed before stop.", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return 2

    _telemetry(
        [
            "snapshot",
            "--project-root",
            str(PROJECT_ROOT),
            "--runtime-family",
            "claude",
            "--emitter",
            "snapshot",
            "--emit",
        ]
    )
    _telemetry(
        [
            "emit",
            "limina_session_completed",
            "--runtime-family",
            "claude",
            "--emitter",
            "claude_stop",
            "--property",
            "result_code=success",
            "--flush",
        ]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
