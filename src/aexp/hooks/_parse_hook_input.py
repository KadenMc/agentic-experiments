"""Shared JSON parser for Claude Code hook payloads.

Handles ``Write``, ``Edit``, and ``MultiEdit`` tool payloads uniformly —
returns the ``(file_path, post_edit_content)`` that the hook logic needs to
inspect. ``Edit`` simulates the single-replace; ``MultiEdit`` applies all
edits in sequence.

Pure-function API; no I/O beyond optionally reading the target file to
simulate edits.
"""
from __future__ import annotations

import json
from pathlib import Path


def _read_existing(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def parse_hook_input(raw: str) -> tuple[str, str]:
    """Parse a Claude Code hook JSON payload.

    Returns ``(file_path, post_edit_content)``. Either field may be empty if
    the input is not a write-like operation or is unparseable.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return "", ""

    if "tool_input" in data:
        data = data["tool_input"]

    fp: str = data.get("file_path", "") or ""
    content: str = data.get("content", "") or ""
    old_string: str = data.get("old_string", "") or ""
    new_string: str = data.get("new_string", "") or ""
    edits: list[dict] = data.get("edits", []) or []

    if not content and old_string and fp:
        existing = _read_existing(fp)
        content = existing.replace(old_string, new_string, 1) if existing else new_string

    if not content and edits and fp:
        existing = _read_existing(fp)
        if existing:
            content = existing
            for edit in edits:
                old = edit.get("old_string", "") or ""
                new = edit.get("new_string", "") or ""
                if old:
                    content = content.replace(old, new, 1)
        else:
            content = "\n".join(edit.get("new_string", "") or "" for edit in edits)

    if not content:
        content = new_string

    return fp, content
