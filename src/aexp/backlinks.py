"""Bidirectional wiki-link maintenance for ``kb/`` artifacts.

``kb_validate`` enforces that a child artifact (``F###``) and its parent
(``H###``, ``E###``) link each other in their ``## Links`` sections. Creating
the child file is one step; patching the parent is a separate edit that
agents forget under pressure. This module owns that edit.

The public surface is ``add_backlink(parent_path, child_id)`` — it parses
the ``## Links`` section of ``parent_path``, adds ``- [[child_id]]`` if
absent, and writes the file back atomically.
"""
from __future__ import annotations

import re
from pathlib import Path

from aexp.utils.atomic import atomic_write

_LINKS_HEADING = "## Links"
# Match [[X]], [[X#anchor]], or [[X|alias]] — we care about the target only.
_WIKILINK_TARGET_RE_TMPL = r"\[\[{target}(?:\]|[|#])"


def _find_links_section(lines: list[str]) -> tuple[int, int] | None:
    """Return ``(start, end)`` line indices for the ``## Links`` section body.

    ``start`` is the line index of the ``## Links`` heading itself; ``end`` is
    the line index of the next ``## `` heading or ``len(lines)``. Returns
    ``None`` if no section is present.
    """
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == _LINKS_HEADING:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return start, end


def _section_has_link(section_text: str, child_id: str) -> bool:
    pattern = _WIKILINK_TARGET_RE_TMPL.format(target=re.escape(child_id))
    return re.search(pattern, section_text) is not None


def add_backlink(parent_path: str | Path, child_id: str) -> bool:
    """Ensure ``parent_path`` lists ``[[child_id]]`` in its ``## Links`` section.

    Returns ``True`` if the file was modified, ``False`` if the link was
    already present. Creates the ``## Links`` section at the end of the file
    if absent.

    The writer preserves the rest of the file byte-for-byte except for:

    - A single new ``- [[<child_id>]]`` line appended after the last non-blank
      line inside the existing section.
    - A new ``## Links\\n\\n- [[<child_id>]]\\n`` block appended if no section
      existed (with a leading blank line to separate from prior content).

    Line endings are normalized to LF — matches ``atomic_write`` and what the
    validator expects.
    """
    path = Path(parent_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    bounds = _find_links_section(lines)
    if bounds is not None:
        start, end = bounds
        section_text = "\n".join(lines[start + 1 : end])
        if _section_has_link(section_text, child_id):
            return False
        # Insert after the last non-blank line inside the section so the new
        # entry sits with the existing bullets, not below trailing blanks.
        insert_at = end
        while insert_at > start + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        new_lines = lines[:insert_at] + [f"- [[{child_id}]]"] + lines[insert_at:]
        trailing = "\n" if text.endswith("\n") else ""
        new_text = "\n".join(new_lines) + trailing
        atomic_write(path, new_text)
        return True

    # No ## Links section at all — append one.
    suffix = "" if text.endswith("\n") else "\n"
    new_text = f"{text}{suffix}\n{_LINKS_HEADING}\n\n- [[{child_id}]]\n"
    atomic_write(path, new_text)
    return True


__all__ = ["add_backlink"]
