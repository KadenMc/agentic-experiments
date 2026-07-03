"""NFS-safe file-lease primitive built on ``os.link`` (no daemon, no dependency).

This is the low-level mutual-exclusion primitive under :class:`aexp.workpool.WorkPool`.
It is deliberately *not* re-exported from :mod:`aexp.utils` -- most callers want
``WorkPool``, not a raw lease (the way airgapped callers want ``RelayClient``, not
``request``). Import it explicitly (``from aexp.utils.linklease import LinkLease``)
only for an advanced use or a test.

Why ``link()`` and not ``open(O_CREAT|O_EXCL)`` or ``flock``
-----------------------------------------------------------
On NFS, ``flock``/``fcntl`` are unreliable, and ``open(2)`` warns that ``O_EXCL``
"is supported only on NFSv3+ ... programs which rely on it for locking will contain
a race condition" (its server-stored verifier is NFSv3-specific, with documented
NFSv4 regressions). The POSIX-atomic primitive that holds across NFS versions is
``link()``: linking a freshly-written unique file onto the lease name either creates
exactly one new directory entry or fails, and the success can be confirmed by the
inode's link count reaching two. This is the qmail/maildir consensus recipe.

The lease is an *efficiency optimization, not a correctness mechanism*
---------------------------------------------------------------------
Callers built on this (e.g. ``WorkPool``) must derive correctness from an atomic
output write plus, where relevant, an idempotent ledger -- never from the lease alone.
Occasional *double-acquire* of one item (under NFS attribute-cache lag or a falsely
broken stale lease) is explicitly acceptable and safe; the lease only makes it rare.
Every residual race below is therefore at worst *wasteful*, never *wrong*.

Key correctness properties (each defends a specific race)
---------------------------------------------------------
- **Acquire is confirmed by the token, not ``st_nlink``.** ``st_nlink`` is a cached
  attribute that can lie under attribute-cache lag, so an ambiguous ``link()`` outcome
  is resolved by reading the lease's owner token (content this process wrote). The
  ``st_nlink == 2`` check is only a fast-path on the happy outcome.
- **Refresh never re-acquires; refresh/release are token-checked.** A heartbeat that
  re-took a lease it found stolen would create an unbounded ownership ping-pong, so
  ``refresh`` simply stops if the token is no longer ours, and ``release`` deletes the
  lease only if the token is still ours (compare-and-delete).
- **Staleness compares server clock to server clock.** ``os.utime(path, None)`` stamps
  the lease mtime with the *server's* now; staleness reads "now" from a touched probe
  file's mtime (also server time), eliminating client/server clock skew. The token's
  ``acquired_ts`` is diagnostic only and is never used to judge staleness.
"""
from __future__ import annotations

import os
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["LinkLease", "LinkLeaseUnsupported", "probe_exclusive_create"]


class LinkLeaseUnsupported(RuntimeError):
    """Raised when the filesystem cannot perform atomic exclusive link-create.

    Carried by :func:`probe_exclusive_create` as a fail-closed startup guard: a
    filesystem that does not enforce ``os.link`` exclusivity (a second link to an
    existing target must raise) cannot host correct leases, so callers should refuse
    to start rather than silently double-process every item.
    """


@dataclass(frozen=True)
class _Token:
    """Parsed lease-file content: ``"<owner_id> <pid> <acquired_ts>"``.

    Equality is exact, so two tokens read from the *same* unchanged lease file compare
    equal while a re-taken lease (new ``owner_id``/``acquired_ts``) does not. The
    ``acquired_ts`` is diagnostic only -- staleness is judged by file mtime, never by
    this field.
    """

    owner_id: str
    pid: int
    acquired_ts: float

    def render(self) -> str:
        """Serialize to the on-disk line (fixed ``.3f`` so re-parse round-trips)."""
        return f"{self.owner_id} {self.pid} {self.acquired_ts:.3f}"

    @classmethod
    def parse(cls, raw: str) -> _Token | None:
        """Parse a lease line; ``None`` if it is malformed (tolerated, never raised)."""
        parts = raw.strip().split()
        if len(parts) != 3:
            return None
        try:
            return cls(owner_id=parts[0], pid=int(parts[1]), acquired_ts=float(parts[2]))
        except ValueError:
            return None


# -- module-level read seams ------------------------------------------------------
# These three wrap the cross-worker *reads* (which on NFS may be served stale from the
# attribute cache). They are module-level functions, not methods, so a test can replace
# them to simulate attribute-cache lag without monkeypatching ``os``. Production reads
# go straight through. WRITES (link/utime/replace) are never wrapped -- they are
# authoritative on the server even when reads lag.


def _exists(path: Path) -> bool:
    """Best-effort existence check (a cross-worker read; may be stale on NFS)."""
    return path.exists()


def _stat_mtime(path: Path) -> float | None:
    """Server-stamped mtime, or ``None`` if the path is gone (a cross-worker read)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_token(path: Path) -> _Token | None:
    """Read+parse a lease token, or ``None`` if absent/malformed (a cross-worker read).

    The lease file is created by ``os.link`` from a fully-written candidate, so it is
    never torn; a parse failure means the file is absent or not a lease, not partial.
    """
    try:
        raw = path.read_text(encoding="ascii")
    except OSError:
        return None
    return _Token.parse(raw)


def _unlink(path: Path) -> None:
    """Unlink, tolerating a missing target and transient Windows sharing errors.

    A peer may have removed it first (``FileNotFoundError`` -> done). On Windows,
    deleting a file another process briefly holds open raises ``PermissionError``
    (WinError 32); it clears within milliseconds, so we retry a few times (the same
    transient-race handling :func:`aexp.utils.atomic.doc_op_with_retry` uses). POSIX/NFS
    -- the real target, where unlink-while-open is legal -- never enters the retry. A
    persistent failure is swallowed: in this design a lingering lease/temp is bounded and
    safe (it stale-expires or is swept), and ``os.link`` (not unlink) is the real arbiter.
    """
    delay = 0.01
    for _ in range(10):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 1.5, 0.1)


class LinkLease:
    """A reclaimable, TTL-leased mutual-exclusion file per opaque ``item_id``.

    One instance serves many items, all under ``lease_dir``. A lease is held until it
    is released, refreshed past its TTL, or judged stale (mtime older than ``ttl``) and
    broken by a peer. See the module docstring for the correctness model.

    Parameters
    ----------
    lease_dir : str or PathLike
        Directory (on the shared filesystem) holding ``<item_id>.lease`` files. Created
        if absent.
    owner_id : str, optional
        Stable per-process owner identity. Defaults to a fresh ``uuid4`` hex. Distinct
        live processes MUST have distinct ``owner_id``\\ s -- sharing one silently
        breaks exclusion. Exposed mainly so tests can simulate specific owners; normal
        callers (and :class:`aexp.workpool.WorkPool`) let it auto-generate.
    ttl : float, optional
        Seconds after which a lease whose mtime has not advanced is reclaimable.
        Decoupled from item duration when a heartbeat refreshes it (see ``WorkPool``).
        Default ``600``.
    log : callable, optional
        ``log(message: str)`` sink for diagnostics (ASCII-only). Defaults to silent.
    """

    def __init__(
        self,
        lease_dir: str | os.PathLike[str],
        *,
        owner_id: str | None = None,
        ttl: float = 600.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.lease_dir = Path(lease_dir)
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        self.owner_id = owner_id if owner_id is not None else uuid.uuid4().hex
        self.ttl = ttl
        self.log = log

    # -- paths ---------------------------------------------------------------------
    def _lease_path(self, item_id: str) -> Path:
        return self.lease_dir / f"{item_id}.lease"

    def _candidate_path(self, item_id: str) -> Path:
        # Per-attempt nonce: a retry (or a reaped worker reusing owner_id) never races
        # its own leftover candidate.
        return self.lease_dir / f".{item_id}.{self.owner_id}.{uuid.uuid4().hex}.tmp"

    # -- clock ---------------------------------------------------------------------
    def _server_now(self) -> float:
        """Server-clock 'now' via a touched probe file; falls back to the local clock.

        Reading now from a server-stamped mtime (rather than ``time.time()``) keeps the
        staleness comparison in a single clock domain, so client/server skew cannot
        cause over-eager breaks. Used only on the contended EEXIST path.
        """
        probe = self.lease_dir / ".clock_probe"
        try:
            probe.touch()
            os.utime(probe, None)
            return probe.stat().st_mtime
        except OSError:
            return time.time()

    @staticmethod
    def _jitter() -> float:
        """Small randomized delay so concurrent breakers of one lease decorrelate."""
        return random.uniform(0.0, 0.05)

    # -- token helpers -------------------------------------------------------------
    def _token(self) -> _Token:
        return _Token(owner_id=self.owner_id, pid=os.getpid(), acquired_ts=time.time())

    def _token_is_mine(self, lease: Path) -> bool:
        tok = _read_token(lease)
        return tok is not None and tok.owner_id == self.owner_id

    def held_by_me(self, item_id: str) -> bool:
        """True iff ``item_id``'s lease currently carries this process's owner token."""
        return self._token_is_mine(self._lease_path(item_id))

    # -- acquire -------------------------------------------------------------------
    def acquire(self, item_id: str) -> bool:
        """Try to acquire ``item_id``'s lease. Returns True iff this process now holds it.

        Idempotent: re-acquiring a lease this process already holds returns True without
        side effects. A single stale lease encountered along the way is broken and the
        acquire retried exactly once; beyond that the call returns False rather than
        spinning.
        """
        lease = self._lease_path(item_id)
        for _ in range(2):  # original attempt + at most one post-stale-break retry
            candidate = self._candidate_path(item_id)
            try:
                candidate.write_text(self._token().render(), encoding="ascii")
            except OSError:
                _unlink(candidate)
                return False
            try:
                result = self._try_link(lease, candidate)
            finally:
                _unlink(candidate)  # the lease is a separate hardlink; only the temp goes
            if result is not None:
                return result
            # result is None -> we broke a stale lease; loop to retry once
        return False

    def _try_link(self, lease: Path, candidate: Path) -> bool | None:
        """One link attempt. True=won, False=lost-to-live-holder, None=broke-stale-retry."""
        try:
            os.link(candidate, lease)
        except FileExistsError:
            return self._handle_eexist(lease)
        except OSError:
            # Link reported an error but, over NFS, may actually have succeeded on a
            # retried RPC. The token -- not the return code, not st_nlink -- decides.
            return self._token_is_mine(lease)
        # Clean success: fast-path confirm by link count *before* the caller unlinks the
        # candidate; if that is ambiguous, fall back to the authoritative token read.
        try:
            if os.stat(candidate).st_nlink == 2:
                return True
        except OSError:
            pass
        return self._token_is_mine(lease)

    def _handle_eexist(self, lease: Path) -> bool | None:
        tok = _read_token(lease)
        if tok is not None and tok.owner_id == self.owner_id:
            return True  # already ours (idempotent re-acquire)
        mtime = _stat_mtime(lease)
        if mtime is None:
            return None  # vanished between link and stat -> retry
        if self._server_now() - mtime > self.ttl:
            if self._break_stale(lease, observed_token=tok, observed_mtime=mtime):
                return None  # broke a dead worker's lease -> retry; link() arbitrates
        return False  # fresh lease held by a live peer

    def _break_stale(
        self, lease: Path, *, observed_token: _Token | None, observed_mtime: float
    ) -> bool:
        """Break a lease still observed stale. Returns True iff it was removed.

        Aborts (returns False) if the token or mtime changed since observation -- a
        refresh or re-take means a live worker owns it now and must not be stolen. The
        subsequent ``acquire`` retry's ``os.link`` is the real arbiter, so two breakers
        that both unlink then both link still yield exactly one winner.
        """
        tok = _read_token(lease)
        mtime = _stat_mtime(lease)
        if mtime is None:
            return True  # already gone
        if tok != observed_token or mtime != observed_mtime:
            return False  # refreshed/re-taken since we observed it -> do not steal
        time.sleep(self._jitter())
        _unlink(lease)
        return True

    # -- heartbeat + release -------------------------------------------------------
    def refresh(self, item_id: str) -> None:
        """Heartbeat: bump our lease's mtime so peers keep seeing it live.

        Token-checked and **never re-acquires**: if the lease is no longer ours (stolen
        after a false stale-break, or vanished), it simply stops -- re-taking would
        create an unbounded ownership ping-pong. A lost lease just means a peer may also
        process the item, which is safe.
        """
        lease = self._lease_path(item_id)
        if not self._token_is_mine(lease):
            if self.log is not None:
                self.log(f"linklease: lease for {item_id} no longer ours; stop refreshing")
            return
        try:
            os.utime(lease, None)  # server-now mtime; never a local-clock value
        except FileNotFoundError:
            if self.log is not None:
                self.log(f"linklease: lease for {item_id} vanished mid-refresh")

    def release(self, item_id: str) -> None:
        """Release ``item_id``'s lease iff this process still owns it (compare-and-delete).

        Skipping the unlink when the token is not ours avoids deleting a lease that was
        already (falsely) reclaimed and re-taken by a peer -- which would amplify
        duplicate processing. The remaining check-then-unlink window is tiny and, if
        lost, only causes a bounded (safe) duplicate.
        """
        lease = self._lease_path(item_id)
        if self._token_is_mine(lease):
            _unlink(lease)

    # -- hygiene -------------------------------------------------------------------
    def sweep_candidates(self) -> int:
        """Unlink orphan ``.*.tmp`` candidates older than ``ttl`` (crash leftovers).

        Returns the number removed. Fresh candidates (a peer mid-acquire) are left
        alone. Cheap to call once at startup.
        """
        removed = 0
        now = time.time()
        for p in self.lease_dir.glob(".*.tmp"):
            try:
                if now - p.stat().st_mtime > self.ttl:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def probe_exclusive_create(run_dir: str | os.PathLike[str]) -> None:
    """Fail-closed startup self-test: does this filesystem do atomic exclusive link-create?

    Verifies, on the actual ``run_dir`` filesystem, that ``os.link`` creates a link with
    ``st_nlink == 2`` and that a *second* ``os.link`` onto the same target raises
    ``FileExistsError``. Raises :class:`LinkLeaseUnsupported` if either fails, so a
    caller can refuse to start rather than silently double-process every item.

    Safe under concurrent startup: every probe path is per-call unique, so N workers
    calling this simultaneously on the same ``run_dir`` (the normal fleet launch)
    never touch each other's files.

    Honest limit: this proves the filesystem supports atomic link-create *at all* (it
    catches a grossly misconfigured mount); it cannot prove cross-node server-side
    exclusivity from a single process. The real cross-node proof is a multi-worker
    smoke on the target cluster.

    Parameters
    ----------
    run_dir : str or PathLike
        A directory on the shared filesystem the leases will live under. A throwaway
        ``_pool/.probe`` subtree is created and cleaned up.

    Raises
    ------
    LinkLeaseUnsupported
        If exclusive link-create is unsupported or not enforced on this filesystem.
    """
    probe_dir = Path(run_dir) / "_pool" / ".probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    # Every path is per-call unique: N workers starting concurrently on one run_dir
    # (the normal WorkPool fleet launch) each self-test their OWN target. A shared
    # target made peers collide -- one worker's link hit another's in-flight target
    # (FileExistsError misread as "unsupported"), or a peer's cleanup unlinked ours
    # mid-test (second link falsely succeeded -> "NOT enforced").
    nonce = uuid.uuid4().hex
    src = probe_dir / f".src_{nonce}"
    src2 = probe_dir / f".src2_{nonce}"
    target = probe_dir / f".probe_{nonce}.lease"
    try:
        src.write_text("probe", encoding="ascii")
        try:
            os.link(src, target)
        except OSError as exc:
            raise LinkLeaseUnsupported(
                f"os.link is unsupported on the filesystem at {run_dir!s}: {exc}"
            ) from exc
        if os.stat(src).st_nlink != 2:
            raise LinkLeaseUnsupported(
                f"os.link did not produce st_nlink==2 at {run_dir!s} "
                "(filesystem does not honor hardlinks correctly)"
            )
        src2.write_text("probe2", encoding="ascii")
        try:
            os.link(src2, target)
        except FileExistsError:
            pass  # correct: exclusive create is enforced
        else:
            raise LinkLeaseUnsupported(
                f"exclusive link-create is NOT enforced at {run_dir!s} "
                "(a second os.link onto an existing target did not raise)"
            )
    finally:
        _unlink(src)
        _unlink(src2)
        _unlink(target)
