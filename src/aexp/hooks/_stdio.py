"""Hooks read and write UTF-8, because Claude Code does.

Claude Code writes a hook's JSON payload to stdin as UTF-8, with non-ASCII
characters left unescaped, and decodes what the hook writes to stdout and
stderr as UTF-8. Python gives a pipe the locale's encoding instead, which on
Windows is cp1252 with ``surrogateescape``. Left alone, a non-ASCII path or
word in the payload is read as mojibake, so a hook looks for a file or a
checkout that does not exist, and printing a character cp1252 lacks raises
``UnicodeEncodeError``.

Keeping hook source ASCII (``tests/test_hooks_ascii.py``) covers what a hook
says itself, not the paths and text it reads and relays.
"""
from __future__ import annotations

import sys


def use_utf8() -> None:
    """Switch stdin, stdout and stderr to UTF-8. Call it before any I/O.

    Stdin decodes with ``replace``, so a malformed byte cannot cost a hook its
    payload. A stream replaced by one without ``reconfigure`` is left alone.
    """
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
