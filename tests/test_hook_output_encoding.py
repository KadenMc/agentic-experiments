"""``session_start`` writes UTF-8, whatever encoding its stdout pipe defaults to.

The hook relays ``kb/mission/CHALLENGE.md`` and ``kb/ACTIVE.md`` verbatim, and
those files may hold any character. ``tests/test_hooks_ascii.py`` keeps hook
*source* ASCII, which covers what a hook says itself but not what it relays.
On Windows a piped stdout defaults to cp1252, so the first character outside
it, such as an emoji, raised ``UnicodeEncodeError`` right after the first
header: the hook exited 1 and the session received neither file.

``PYTHONIOENCODING=cp1252`` imposes that Windows default on any platform, so
this test fails on the unpatched hook under Linux as well.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

WARNING_SIGN = "\u26a0\ufe0f"  # U+26A0 U+FE0F, the emoji form of the warning sign
AT_LEAST = "\u2265"  # U+2265 GREATER-THAN OR EQUAL TO


@pytest.fixture
def project(scaffold_project: Path) -> Path:
    """A checkout whose CHALLENGE.md holds two characters cp1252 cannot encode."""
    subprocess.run(["git", "init", "-q", str(scaffold_project)], check=True, capture_output=True)
    challenge = scaffold_project / "kb" / "mission" / "CHALLENGE.md"
    note = f"\n{WARNING_SIGN} Every claim needs n {AT_LEAST} 3 runs.\n"
    challenge.write_text(challenge.read_text(encoding="utf-8") + note, encoding="utf-8")
    return scaffold_project


def test_session_start_relays_characters_cp1252_cannot_encode(project: Path) -> None:
    """Before: it printed the first header, then exited 1 on ``UnicodeEncodeError``."""
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), PYTHONIOENCODING="cp1252")
    r = subprocess.run(
        [sys.executable, "-m", "aexp.hooks.session_start"],
        input=json.dumps({"cwd": str(project)}).encode(),
        capture_output=True,
        check=False,
        timeout=60,
        cwd=str(project),
        env=env,
    )
    assert r.returncode == 0, (r.returncode, r.stderr.decode(errors="replace"))
    out = r.stdout.decode("utf-8")
    assert WARNING_SIGN in out
    assert AT_LEAST in out
    assert "=== kb/ACTIVE.md ===" in out
