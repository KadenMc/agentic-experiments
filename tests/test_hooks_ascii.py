"""Every ``aexp.hooks`` source file must be plain ASCII.

Claude Code runs each hook as a ``python -m aexp.hooks.<name>`` subprocess.
On Windows, a plain ``print()`` to a pipe defaults to the console's cp1252
encoding rather than UTF-8, and that encode raises ``UnicodeEncodeError`` the
moment the string contains a character outside ASCII -- an emoji, box-drawing
glyphs, curly quotes, or an en/em dash are all common ways prose picks one up
without anyone noticing. This has already broken a hook in practice (see
``tests/test_jupyter_connect_postuse.py``).

Restricting hook *source files* to ASCII is a stronger, simpler guarantee
than auditing every string a hook might print: it removes the character
class entirely rather than relying on each new line of code to avoid it.
"""
from __future__ import annotations

import pathlib

import pytest

import aexp.hooks

_HOOKS_DIR = pathlib.Path(aexp.hooks.__file__).parent
_HOOK_SOURCE_FILES = sorted(_HOOKS_DIR.rglob("*.py"))


def test_hook_source_files_were_discovered() -> None:
    """Guard the guard: an empty glob would make the test below vacuous."""
    assert _HOOK_SOURCE_FILES, f"no .py files discovered under {_HOOKS_DIR}"


@pytest.mark.parametrize(
    "path",
    _HOOK_SOURCE_FILES,
    ids=[str(p.relative_to(_HOOKS_DIR)) for p in _HOOK_SOURCE_FILES],
)
def test_hook_source_is_ascii_only(path: pathlib.Path) -> None:
    """Fails with the offending byte's position if a file has drifted off ASCII."""
    raw = path.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{path} is not ASCII-only: {exc}")
