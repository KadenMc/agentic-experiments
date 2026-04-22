#!/usr/bin/env python3
"""Shared JSON parser for Limina hooks.

Importable module **and** a CLI fallback:

- As a module: ``from _parse_hook_input import parse_hook_input``.
- As a CLI: reads JSON from stdin, prints ``<file_path>\\n<content>`` to stdout.
  Preserved so the vendored copy continues to behave identically to the
  upstream shell-hook invocation pattern if anything still calls it directly.

For Write: uses the full content.
For Edit: simulates the edit by applying old_string -> new_string on the existing file.
For MultiEdit: applies all edits sequentially to produce the post-edit content.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _read_existing(path: str) -> str:
    """Try to read the existing file content. Return ``""`` on failure."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def parse_hook_input(raw: str) -> tuple[str, str]:
    """Parse a Claude Code hook JSON payload.

    Parameters
    ----------
    raw : str
        The raw JSON text from ``stdin`` (or equivalent).

    Returns
    -------
    tuple[str, str]
        ``(file_path, post_edit_content)``. Either may be empty if the input
        is unparseable or does not describe a write-like operation.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return "", ""

    # PostToolUse payloads nest the tool_input and may carry a huge tool_response;
    # discard the response wrapper immediately.
    if "tool_input" in data:
        data = data["tool_input"]

    fp: str = data.get("file_path", "") or ""
    content: str = data.get("content", "") or ""
    old_string: str = data.get("old_string", "") or ""
    new_string: str = data.get("new_string", "") or ""
    edits: list[dict] = data.get("edits", []) or []

    # Edit operation: simulate the single-replace to get post-edit content.
    if not content and old_string and fp:
        existing = _read_existing(fp)
        content = existing.replace(old_string, new_string, 1) if existing else new_string

    # MultiEdit operation: apply all edits sequentially.
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

    # Fallback: if still no content, use new_string directly.
    if not content:
        content = new_string

    return fp, content


def main() -> int:
    """CLI fallback: read stdin, print file_path + content to stdout."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    fp, content = parse_hook_input(raw)
    print(fp)
    print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
