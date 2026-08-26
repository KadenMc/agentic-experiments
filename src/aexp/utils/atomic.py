"""Atomic file writes.

``atomic_write`` materializes new content in a **per-writer** temp sibling and then
calls ``Path.replace``. On POSIX this is atomic; on Windows it's as atomic as
the filesystem allows (NTFS guarantees the replace is all-or-nothing for files
opened without sharing).

The temp name carries a nonce, and that is load-bearing
-------------------------------------------------------
``replace`` being atomic only makes the *publish* indivisible; it says nothing about
who filled the file being published. A temp path derived from the destination alone is
shared by every concurrent writer of that destination, so the interleaving it exists to
prevent simply moves upstream from ``dest`` to ``dest.tmp``: two writers open one temp
with ``O_TRUNC``, write at independent offsets, and each then publishes whatever the
other left behind -- a destination holding one writer's payload spliced onto another's.

This was measured, not reasoned about. A/B against this module's own pre-nonce body on
one Linux 5.14 host (xfs and NFS, 2 and 4 concurrent writers, three payload-flush
shapes, 150 destinations each -- 1350 per implementation):

===================  ==========================  ==========================
leg                  destinations torn           ``replace`` raised
===================  ==========================  ==========================
shared ``.tmp``      751 / 1350, every config    ~1 per trial, every config
per-writer nonce     0 / 1350                    none
===================  ==========================  ==========================

Per-config tear rates ranged from 1.3% to 98% -- they swing with filesystem, writer
count, payload size and machine load, so no single rate is the number; what is stable is
that every configuration tore and that none did afterwards. NFS is not immune: run
directly against the *installed* pre-nonce function on the plain ``write_text`` path,
four concurrent writers tore 48% and 77% of destinations in two runs. Two writers there
is the narrowest shape -- 0%, 0% and 1.3% across three runs -- and the mount says why:
``wsize`` was 512 KB, so ``write_text`` handing a 256 KB payload to one ``write`` crossed
the wire as a single RPC. That is the payload fitting inside ``wsize``, not immunity; a
larger record splits into the multi-RPC shape that tore everywhere.

Windows differs only in being loud: its mandatory file sharing refuses the concurrent
open with ``PermissionError`` instead of interleaving, which is still a lost write -- and
once each writer has its own temp, the same mandatory sharing refuses the concurrent
*rename* instead, which is why the replace below is wrapped in
:func:`doc_op_with_retry`.

A per-call nonce makes each writer's temp private, so both failure modes collapse into
the behavior callers already expect: **last writer wins, whole**. This matters most to
:class:`aexp.workpool.WorkPool`, which explicitly permits occasional double-processing
of an item and rests its whole termination proof on the caller's output write being
atomic -- a torn output drives ``is_done`` true over corrupt content, monotonically and
irreversibly. Same reasoning, same nonce, as
:meth:`aexp.utils.linklease.LinkLease._candidate_path`.
"""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


def doc_op_with_retry(
    fn: Callable[[], Any],
    *,
    attempts: int = 10,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> Any:
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

    Safe under concurrent writers of the same destination: each call stages into its
    own temp sibling, so a reader of ``path`` sees one writer's complete content and
    never a splice of two. Which writer wins is unspecified (the last ``replace``),
    which is the guarantee callers such as :class:`aexp.workpool.WorkPool` need.

    Parameters
    ----------
    path : str or PathLike
        Destination path.
    content : str or bytes
        Content to write. ``bytes`` bypasses encoding/newline handling.
    encoding : str, optional
        Text encoding (ignored for bytes). Default ``"utf-8"``.
    newline : str or None, optional
        Newline policy (ignored for bytes). Default ``"\n"`` to force LF
        on Windows as well — matters because the bundled scaffold files
        and kb_validate expect Unix line endings.

    Returns
    -------
    Path
        The resolved destination path.

    Notes
    -----
    The temp sibling is named ``<dest>.<pid>.<nonce>.tmp`` and is unlinked if the
    write or the replace raises, so a failed call leaves no orphan behind — without
    that cleanup a unique name would accumulate one per failure, where the old shared
    name was self-clobbering. The replace is retried via :func:`doc_op_with_retry` so
    that concurrent writers do not collide on Windows. See the module docstring for
    why the nonce is required.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # pid is for the human reading an orphan left by a hard kill; the uuid is what
    # actually makes the name unique (two threads of one process share a pid).
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")

    try:
        if isinstance(content, bytes):
            tmp.write_bytes(content)
        else:
            tmp.write_text(content, encoding=encoding, newline=newline)
        # Retried, because giving each writer its own temp moves the last remaining
        # Windows collision from the open to the rename: two processes replacing over
        # one destination raise ``PermissionError [WinError 5]`` there. That is the
        # exact transient ``doc_op_with_retry`` above exists for, and on POSIX --
        # where overlapping renames are atomic and never raise -- the first attempt
        # always succeeds, so the wrapper costs nothing.
        doc_op_with_retry(lambda: tmp.replace(dest))
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt mid-write is exactly the
        # case that would strand a uniquely-named temp forever.
        tmp.unlink(missing_ok=True)
        raise
    return dest
