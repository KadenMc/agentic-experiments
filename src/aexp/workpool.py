"""Daemonless work-stealing pool over a shared filesystem (opt-in).

``WorkPool`` lets N **independently-launched** workers (e.g. separate SLURM/Jupyter GPU
jobs) cooperatively drain one body of work off a shared filesystem with **no daemon,
no broker, no scheduler** -- the niche where redis/celery/dask (which need a persistent
process) are disqualified and SLURM job arrays don't apply (the jobs are already
running, launched interactively/by an agent, and churn: they join late and die on
walltime/VPN). Each item is claimed with an NFS-safe :class:`~aexp.utils.linklease.LinkLease`;
a dead worker's claim goes stale and is reclaimed by a peer.

This module is **not** imported at ``aexp`` package init (like ``aexp.airgapped``): the
constraint set is real but specific, so importing it is opt-in
(``from aexp.workpool import WorkPool``).

workpool vs ``aexp.queue``
--------------------------
They are orthogonal and compose; pick by granularity:

- ``aexp.queue`` -- **coarse, inter-run, single-driver**: register N *signac runs* on
  one machine, materialize a runner, one process iterates them. It is about run
  *provenance and registration*; no concurrency model, no stale-reclaim.
- ``aexp.workpool`` -- **fine, intra-run, multi-driver**: many already-running workers
  steal *items within one run* off a shared FS, with liveness/reclaim/contention as the
  whole point; no signac, no provenance. A single queued run could internally use a
  ``WorkPool``.

Correctness model (read before relying on it)
---------------------------------------------
The lease is an **efficiency optimization, not the correctness mechanism**. Correctness
must come from the caller's ``process`` writing its output **atomically** (so a dead
worker never leaves a torn file) and, where a shared tally exists, an **idempotent
ledger**. Occasional double-*processing* of one item (under NFS attribute-cache lag or a
falsely-broken stale lease) is explicitly acceptable and safe -- the lease only makes it
rare. The pool therefore guarantees **completeness and liveness**, not zero-duplicate
processing.

Three caller invariants the signature cannot express:

1. ``is_done(item)`` MUST be **monotonic** -- once true it stays true -- and become true
   only as a **durable effect of a completed** ``process(item)`` **or of**
   ``on_exhausted(item)`` (canonically: an atomically-written output file exists). This is
   what makes block-and-retry termination safe: a stale-*negative* ``is_done`` only delays
   exit (safe), and a stale-*positive* cannot happen because outputs are never deleted. An
   ``is_done`` that can flip back to false (a lock, a rolled-back row, a cleaned temp
   file) breaks the termination proof.
2. ``item_id`` MUST be a **filesystem-safe basename** (no ``/`` or ``\\``, not ``.``/
   ``..``) -- it names a lease file.
3. ``on_exhausted`` is the **only** terminal handler, and every item that stops being
   worked on goes through it. ``on_error`` is a diagnostic hook, not a terminal one:
   whatever it writes, the pool still exhausts the item. Writing a *sidecar* from
   ``on_error`` and expecting the pool to move on is the mistake this module used to
   permit silently -- the sidecar is not what ``is_done`` checks, so the item was reclaimed
   forever. ``run`` now refuses an ``on_error`` without an ``on_exhausted``, and
   ``on_exhausted``'s promise is verified rather than trusted.
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from aexp.utils.linklease import LinkLease, probe_exclusive_create

__all__ = ["WorkPool", "probe_exclusive_create"]


class WorkPool:
    """A per-item, lease-based work-stealing pool over a shared directory.

    Construct one per worker process over the **same** ``item_ids`` and ``lease_dir`` on
    the shared filesystem; call :meth:`run` (recommended) or drive
    :meth:`claim_next`/:meth:`mark_done` manually. ``is_done`` is the caller's
    output-existence predicate -- it is both the skip check and the global termination
    condition, so the filesystem and the pool agree by construction.

    Parameters
    ----------
    item_ids : sequence of str
        The full item universe (every worker passes the same list). Each must be a
        filesystem-safe basename (see the module docstring).
    is_done : callable
        ``is_done(item_id) -> bool``, true once the item's durable output exists. Must be
        monotonic (see the module docstring).
    lease_dir : str or PathLike
        Directory on the shared filesystem for the ``<item_id>.lease`` files.
    ttl : float, optional
        Lease staleness horizon in seconds (default ``600``). A live worker keeps its
        lease fresh via the heartbeat, so ``ttl`` is decoupled from item duration -- it
        only governs how fast a *dead* worker's item is reclaimed.
    heartbeat : float, optional
        Refresh period in seconds for the active lease. Defaults to ``ttl / 5`` (tolerate
        ~4 missed beats before a peer judges the lease stale).
    backoff : tuple of (float, float), optional
        ``(min, max)`` seconds for the exponential, jittered poll wait used when nothing
        is currently claimable but the work is not globally done. Default ``(1.0, 30.0)``.
    log : callable, optional
        ``log(message: str)`` ASCII-only sink for progress/diagnostics. Defaults silent.
    max_attempts : int, optional
        Retry budget for a **retryable** failure (default ``1`` = no retry). A retryable
        exception (see ``retryable``) writes no output, so ``is_done`` stays false and the
        item is reclaimed and retried -- by any worker, so retry spans the fleet (a heavy
        item that OOMs a small GPU can be re-run on a bigger one). This bounds retries
        only; it does **not** decide whether a failure terminates. A *non*-retryable
        failure has a budget of 1 and exhausts on first occurrence, whatever this is set
        to. Worker *death* (no exception) is orthogonal -- always reclaimed via the stale
        lease, never counted as an attempt.
    on_exhausted : callable, optional
        ``on_exhausted(item_id) -> None``, called once an item has spent its budget --
        ``max_attempts`` retryable failures, or a single non-retryable one. It MUST make
        ``is_done(item_id)`` true durably (e.g. write an excluded/void output); that is
        what stops the item being reclaimed forever and lets the pool terminate (the same
        role a successful ``process`` output plays), and :meth:`_verify_terminal` checks
        that it did. Should be idempotent (a rare double-exhaust under lag must be safe),
        like ``process``. **Required when ``max_attempts > 1``** (enforced here) and
        whenever ``run`` is given an ``on_error`` (enforced in :meth:`run`).
    retryable : exception type or tuple of types, optional
        Restrict *retrying* to these exception types (default ``None`` = every
        ``Exception`` is retryable once ``max_attempts > 1``). An out-of-scope exception
        is still reported to ``on_error`` and still terminates through ``on_exhausted`` --
        it simply gets one attempt instead of ``max_attempts``.

    Notes
    -----
    ``owner_id`` is internal (auto ``uuid4`` per process) on purpose: a shared/public
    owner id silently breaks mutual exclusion.
    """

    def __init__(
        self,
        *,
        item_ids: Sequence[str],
        is_done: Callable[[str], bool],
        lease_dir: str | Path,
        ttl: float = 600.0,
        heartbeat: float | None = None,
        backoff: tuple[float, float] = (1.0, 30.0),
        log: Callable[[str], None] | None = None,
        max_attempts: int = 1,
        on_exhausted: Callable[[str], None] | None = None,
        retryable: type[BaseException] | tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self._item_ids: list[str] = list(item_ids)
        self._validate_ids(self._item_ids)
        self._is_done = is_done
        self._owner_id = uuid.uuid4().hex
        self._lease = LinkLease(lease_dir, owner_id=self._owner_id, ttl=ttl, log=log)
        self._ttl = ttl
        self._heartbeat = heartbeat if heartbeat is not None else ttl / 5.0
        self._backoff_min, self._backoff_max = backoff
        self._log = log

        # Retry-on-failure (opt-in; max_attempts == 1 reproduces today exactly -- no
        # attempt counting, no exhaustion, an exception routes straight to on_error).
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if max_attempts > 1 and on_exhausted is None:
            raise ValueError(
                "max_attempts > 1 enables retry-on-failure, so on_exhausted is "
                "required: when an item has failed max_attempts times the pool calls "
                "on_exhausted(item), which MUST make is_done(item) true durably (e.g. "
                "write an excluded/void output). Without it a permanently-failing item "
                "would be reclaimed forever and the pool would never terminate."
            )
        self._max_attempts = max_attempts
        self._on_exhausted = on_exhausted
        self._retryable = retryable
        # Per-item attempt tally lives beside the leases (its own dir so it never
        # collides with <item>.lease). Created lazily on the first failure.
        self._attempts_dir = Path(lease_dir).parent / "_attempts"

        self._active_item: str | None = None
        self._lock = threading.Lock()  # guards _active_item across the heartbeat thread
        self._stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

        # Per-process rotation offset so workers start at different points in the list
        # and rarely contend on the same item. Rotation (not strided) always covers the
        # whole list regardless of any common factor with len(item_ids).
        self._start_offset = (
            hash(self._owner_id) % len(self._item_ids) if self._item_ids else 0
        )
        self._lease.sweep_candidates()  # clear crash-orphaned candidate temps once

    @staticmethod
    def _validate_ids(ids: list[str]) -> None:
        for i in ids:
            if not i or "/" in i or "\\" in i or i in (".", ".."):
                raise ValueError(
                    f"item_id must be a non-empty filesystem-safe basename, got {i!r}"
                )

    # -- context management (heartbeat lifecycle) ----------------------------------
    def __enter__(self) -> WorkPool:
        self._ensure_heartbeat()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=self._heartbeat + 1.0)

    def _ensure_heartbeat(self) -> None:
        if self._hb_thread is None:
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name="workpool-heartbeat", daemon=True
            )
            self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        # _stop.wait returns True when stop is set (clean shutdown), False on timeout
        # (time for a beat). Refresh outside the lock so mark_done never blocks on I/O.
        while not self._stop.wait(self._heartbeat):
            with self._lock:
                item = self._active_item
            if item is None:
                continue
            try:
                self._lease.refresh(item)
            except Exception as exc:  # a dead heartbeat must be visible, never silent
                if self._log is not None:
                    self._log(f"workpool: heartbeat refresh error for {item}: {exc}")

    # -- the advertised driver -----------------------------------------------------
    def run(
        self,
        process: Callable[[str], None],
        *,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        """Drain the pool: claim -> ``process`` -> release, until globally done.

        This owns the load-bearing protocol so adopters never re-derive it: the lease is
        released in a ``finally`` (so a failing item is reclaimed, not stranded), and the
        heartbeat thread is stopped on exit. ``process(item_id)`` does the work and MUST
        write its output atomically (the output, not this call, is the done-marker).

        Parameters
        ----------
        process : callable
            ``process(item_id) -> None``. Exceptions are routed to ``on_error`` if given,
            else re-raised. ``KeyboardInterrupt``/``SystemExit`` always propagate (after
            the lease is released) so the worker can be stopped.
        on_error : callable, optional
            ``on_error(item_id, exc)`` -- a place to record a diagnostic, e.g. an error
            sentinel naming the exception. It is **not** a terminal handler: whatever it
            writes, the item is then exhausted through ``on_exhausted``, which is what
            makes ``is_done`` true. Supplying ``on_error`` without ``on_exhausted`` is
            therefore rejected (see Raises). If both are ``None``, an exception aborts
            ``run`` unchanged.

        Raises
        ------
        ValueError
            If ``on_error`` is supplied without ``on_exhausted``. That pairing cannot
            terminate: a failure routed only to ``on_error`` writes no done-marker, so
            ``is_done`` stays false and the item is reclaimed forever. Deliberately
            breaking -- it converts a silent infinite loop into a startup error naming the
            fix. Callers driving :meth:`claim_next`/:meth:`mark_done` by hand handle their
            own exceptions and are outside this mechanism entirely.
        """
        if on_error is not None and self._on_exhausted is None:
            raise ValueError(
                "on_error is supplied but on_exhausted is not. A failure routed to "
                "on_error writes no done-marker, so is_done stays false and the item is "
                "reclaimed forever. Pass on_exhausted(item) -- it MUST make "
                "is_done(item) true durably."
            )
        with self:
            total = len(self._item_ids)
            t0 = time.time()
            worker_done = 0
            while (item := self.claim_next()) is not None:
                try:
                    process(item)
                except Exception as exc:  # noqa: BLE001 -- routed by contract below
                    if not self._handle_failure(item, exc, on_error):
                        raise
                finally:
                    self.mark_done(item)
                worker_done += 1
                self._log_progress(worker_done, total, t0)

    def _handle_failure(
        self,
        item_id: str,
        exc: Exception,
        on_error: Callable[[str, Exception], None] | None,
    ) -> bool:
        """Route a ``process`` failure. Returns True if handled, False to propagate.

        **Every** failure gets a budget and a terminal state. Retryable-vs-not decides *how
        many attempts*, not *bounded vs unbounded*: retrying a non-retryable failure is
        pointless, so its budget is 1 -- but a budget of 1 is still a budget, and it still
        ends in ``on_exhausted``. Previously a non-retryable exception went to ``on_error``
        and nothing else, which records no attempt, has no budget, and carries no
        terminality requirement, so ``is_done`` stayed false and the item was reclaimed
        forever. `75ea8ff` believed it had closed that livelock; it closed it only on the
        retryable path.

        A retryable failure still writes NO output, so the item is reclaimed and re-run --
        by this worker or a peer, so retry stays cross-worker for free.

        Preserved exactly: ``on_error`` still fires for non-retryable failures (callers
        keep their diagnostic sentinel) and still does not fire for retryable ones; worker
        *death* remains orthogonal (stale lease, never an attempt); and with neither
        callback supplied a hard exception still propagates and kills the worker.
        """
        retryable = self._is_retryable(exc)

        # Gate on on_exhausted, NOT on on_error. Without a terminal handler there is
        # nothing to exhaust TO -- and ``on_exhausted`` is None in the default
        # configuration, since __init__ only requires it once max_attempts > 1. Calling it
        # unconditionally would raise TypeError from inside an except handler and mask the
        # real exception. A caller who supplied on_exhausted has opted into
        # terminalization; one who supplied neither keeps today's propagate-and-die.
        if not retryable and self._on_exhausted is None:
            # `run()` rejects this pairing up front, so the branch below is reachable only
            # for a caller driving claim_next()/mark_done() by hand.
            if on_error is not None:
                on_error(item_id, exc)
                return True
            return False

        if not retryable and on_error is not None:
            on_error(item_id, exc)

        budget = self._max_attempts if retryable else 1
        n = self._record_attempt(item_id)
        if n >= budget:
            # A peer may have completed this item while we were failing -- the pool
            # explicitly permits occasional double-processing. Exhausting now would call a
            # terminal writer over a real result, and the two shipped writers in the wild
            # both overwrite rather than skip. Same TOCTOU re-check `_scan_once` already
            # performs after winning a lease.
            if self._is_done(item_id):
                # Either a peer finished it, or this caller's own on_error wrote the
                # done-marker. Both mean the item is terminal already and exhausting would
                # write over it.
                self._emit(f"workpool: {item_id} is already done (peer, or on_error wrote "
                           "the output); skipping on_exhausted")
                return True
            self._emit(
                f"workpool: {item_id} exhausted after {n} attempt(s) "
                f"({type(exc).__name__}); calling on_exhausted"
            )
            # Non-None on both paths into here: a retryable failure implies
            # ``max_attempts > 1``, which ``__init__`` refuses without ``on_exhausted``;
            # a non-retryable one returned above when it was None.
            assert self._on_exhausted is not None
            self._on_exhausted(item_id)
            self._verify_terminal(item_id)
        else:
            self._emit(
                f"workpool: {item_id} attempt {n}/{budget} failed "
                f"({type(exc).__name__}); will retry"
            )
        return True

    def _verify_terminal(self, item_id: str) -> None:
        """``on_exhausted`` promised ``is_done``; check that it delivered.

        Nothing used to check, which is exactly how a consumer's earlier livelock went
        silent: its exclusion marker was written under one filename while ``is_done``
        globbed another, so the pool reclaimed an "excluded" item forever.

        Raising immediately on a false ``is_done`` would be wrong for this filesystem --
        NFS attribute-cache lag produces stale negatives, and the module's correctness
        model is built on "a stale-negative ``is_done`` only delays exit (safe)". A hard
        raise would turn benign lag into a dead worker.

        So confirmation is **durable and fleet-wide** rather than per-process: a failed
        verification drops an ``exhausted_<uuid>`` marker beside the attempt counters, and
        the second one anywhere raises. A per-process set could not escalate here at all --
        these workers join late and die on walltime, so each fresh process would warn once
        and exit, and the livelock would return as warn-spam with a full ``process()`` run
        burned per cycle.

        Raising is the right terminal state once terminality is provably broken. Skipping
        the item locally leaves ``_all_done`` false, so ``claim_next`` spins forever -- the
        livelock renamed. Treating it as done locally lets the pool exit with items neither
        produced nor excluded, which is a silent corpus shrink.
        """
        if self._is_done(item_id):
            return
        d = self._attempts_dir / item_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"exhausted_{uuid.uuid4().hex}").write_text(self._owner_id, encoding="ascii")
        if sum(1 for p in d.iterdir() if p.name.startswith("exhausted_")) >= 2:
            raise RuntimeError(
                f"on_exhausted({item_id!r}) has run at least twice across the fleet and "
                "is_done is still false; the pool would reclaim this item forever. "
                "on_exhausted MUST write the durable output that is_done checks."
            )
        self._emit(f"workpool: WARNING {item_id} exhausted but is_done still false "
                   "(filesystem lag, or on_exhausted did not write the done-marker)")

    def _emit(self, msg: str) -> None:
        if self._log is not None:
            self._log(msg)

    def _is_retryable(self, exc: Exception) -> bool:
        """True iff retry is enabled (max_attempts > 1) and ``exc`` is in scope."""
        if self._max_attempts <= 1:
            return False
        if self._retryable is None:
            return True
        return isinstance(exc, self._retryable)

    def _record_attempt(self, item_id: str) -> int:
        """Record one failed attempt and return the running count (cross-worker durable).

        Each attempt is a uniquely-named marker under ``_attempts/<item_id>/`` -- so
        counting is lag-tolerant and needs no read-modify-write (matching the pool's
        NFS-safety model). Reached for **every** failure, retryable or not: a
        non-retryable one simply has a budget of 1 (see :meth:`_handle_failure`).

        ``exhausted_*`` markers share this directory but are :meth:`_verify_terminal`'s,
        not attempts, and are excluded from the count -- otherwise a failed verification
        would inflate the next attempt number and could retire a retry budget early.
        """
        d = self._attempts_dir / item_id
        d.mkdir(parents=True, exist_ok=True)
        (d / uuid.uuid4().hex).write_text(self._owner_id, encoding="ascii")
        return sum(1 for p in d.iterdir() if not p.name.startswith("exhausted_"))

    def _log_progress(self, worker_done: int, total: int, t0: float) -> None:
        if self._log is None:
            return
        done, _ = self.progress()
        elapsed = time.time() - t0
        rate = (worker_done / elapsed * 60.0) if elapsed > 0 else 0.0
        remaining = total - done
        eta = (remaining / (worker_done / elapsed)) if (elapsed > 0 and worker_done) else 0.0
        self._log(
            f"workpool: {done}/{total} done globally; this worker {worker_done} "
            f"at {rate:.1f}/min, ETA ~{eta:.0f}s"
        )

    # -- progress ------------------------------------------------------------------
    def progress(self) -> tuple[int, int]:
        """Return ``(done, total)`` via a cheap ``is_done`` sweep over all items."""
        done = sum(1 for i in self._item_ids if self._is_done(i))
        return done, len(self._item_ids)

    # -- claim / done (advanced manual surface; run() is built on these) -----------
    def claim_next(self) -> str | None:
        """Claim and return the next undone item, or ``None`` iff ALL items are done.

        Block-and-retry: when nothing is currently claimable but the work is not globally
        done (e.g. the only item left is held by a dead-but-not-yet-stale worker), this
        sleeps with jittered exponential backoff and rescans -- it never exits early. On
        success it starts/keeps the heartbeat refreshing the returned item until
        :meth:`mark_done`.
        """
        self._ensure_heartbeat()
        delay = self._backoff_min
        while True:
            item = self._scan_once()
            if item is not None:
                return item
            if self._all_done():
                return None
            time.sleep(self._jittered(delay))
            delay = min(delay * 2.0, self._backoff_max)

    def _scan_once(self) -> str | None:
        for item in self._rotated_order():
            if self._is_done(item):
                continue
            if not self._lease.acquire(item):
                continue
            # Won the lease. Re-check is_done to close the claim/is_done TOCTOU (a peer
            # may have finished between our is_done and our acquire). This closes the
            # fast case; attribute-cache lag can still defeat it, which is safe.
            if self._is_done(item):
                self._lease.release(item)
                continue
            with self._lock:
                self._active_item = item
            return item
        return None

    def mark_done(self, item_id: str) -> None:
        """Stop heartbeating ``item_id`` and release its lease (success or failure).

        Idempotent w.r.t. ownership: the underlying release is a compare-and-delete, so
        if the lease was reclaimed by a peer this is a no-op.
        """
        with self._lock:
            if self._active_item == item_id:
                self._active_item = None
        self._lease.release(item_id)

    # -- helpers -------------------------------------------------------------------
    def _all_done(self) -> bool:
        return all(self._is_done(i) for i in self._item_ids)

    def _rotated_order(self) -> list[str]:
        off = self._start_offset
        return self._item_ids[off:] + self._item_ids[:off]

    @staticmethod
    def _jittered(delay: float) -> float:
        # +/-50% jitter so workers waking at a stale lease's expiry do not thunder.
        return delay * random.uniform(0.5, 1.5)
