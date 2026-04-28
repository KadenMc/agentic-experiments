"""Atomic file writes.

``atomic_write`` materializes new content in a sibling ``.tmp`` file and then
calls ``Path.replace``. On POSIX this is atomic; on Windows it's as atomic as
the filesystem allows (NTFS guarantees the replace is all-or-nothing for files
opened without sharing).
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path


def doc_op_with_retry[T](
    fn: Callable[[], T],
    *,
    attempts: int = 10,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> T:
    """Run a signac-doc operation with retry on Windows file-rename races.

    signac's atomic-rename doc writes (and the corresponding loads) collide
    on Windows when two processes or threads touch the same
    ``signac_job_document.json`` at the same instant. The collision surfaces
    as ``PermissionError [WinError 5]`` (rename-over a locked target) or
    ``PermissionError [Errno 13]`` (open-for-read while another writer holds
    the file). Both are transient: the underlying file lock releases within
    milliseconds. A short retry loop with mild exponential backoff resolves
    the race in practice.

    POSIX ``rename`` doesn't suffer this — overlapping renames are atomic
    and don't raise — but the retry is a no-op there because the first
    attempt always succeeds. Cheap to apply universally.

    Caller is responsible for catching any ``PermissionError`` that escapes
    after ``attempts`` retries (which would indicate a genuinely stuck file
    handle, not a transient race).
    """
    last_exc: PermissionError | None = None
    delay = base_delay
    for _ in range(attempts):
        try:
            return fn()
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay)
            delay = min(delay * 1.5, max_delay)
    if last_exc is not None:
        raise last_exc
    return fn()  # pragma: no cover — unreachable when attempts >= 1


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
