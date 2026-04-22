"""Atomic file writes.

``atomic_write`` materializes new content in a sibling ``.tmp`` file and then
calls ``Path.replace``. On POSIX this is atomic; on Windows it's as atomic as
the filesystem allows (NTFS guarantees the replace is all-or-nothing for files
opened without sharing).
"""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write(
    path: str | os.PathLike[str],
    content: str | bytes,
    *,
    encoding: str = "utf-8",
    newline: str | None = "\n",
) -> Path:
    """Write ``content`` to ``path`` atomically.

    Parameters
    ----------
    path : str or PathLike
        Destination path.
    content : str or bytes
        Content to write. ``bytes`` bypasses encoding/newline handling.
    encoding : str, optional
        Text encoding (ignored for bytes). Default ``"utf-8"``.
    newline : str or None, optional
        Newline policy (ignored for bytes). Default ``"\\n"`` to force LF
        on Windows as well — matters because vendored hook scripts and
        kb_validate expect Unix line endings.

    Returns
    -------
    Path
        The resolved destination path.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")

    if isinstance(content, bytes):
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding=encoding, newline=newline)

    tmp.replace(dest)
    return dest
