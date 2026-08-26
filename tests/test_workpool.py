"""Cross-process hammer for `aexp.workpool.WorkPool` (the load-bearing test).

Asserts the THREE properties the pool actually provides under contention + simulated
NFS attribute-cache lag -- NOT strict exactly-once *processing* (the design permits rare
double-processing and calls it safe):

1. Completeness (hard, every run): every item ends with exactly one parseable output.
2. Single durable record (hard): one output file per item (atomic write -> last wins).
3. Bounded duplicates (hard cap + soft rate): no ping-pong; duplicates rare, not zero
   across the ensemble (proves the lag path was actually exercised).
4. Liveness (hard): every worker process exits within a timeout (no livelock).

Real files on the local FS back the leases (so `os.link` arbitration uses truth);
lag is injected ONLY on the read seams (`linklease._stat_mtime/_read_token/_exists`)
and the test's own `is_done` (stale-negative), modelling that *reads* lag while *writes*
are authoritative. Worker "death" is simulated with `os._exit` mid-item (holding a
lease) to exercise stale-reclaim + block-and-retry. Mirrors the spawn structure of a
consumer's own cross-process ledger test.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import random
import threading
import time
from pathlib import Path

import pytest

from aexp.utils import linklease
from aexp.utils.atomic import atomic_write
from aexp.workpool import WorkPool


# ------------------------------------------------------------------ lag/chaos shim
def _install_read_lag(
    rnd: random.Random, p_lag: float, p_linkfail: float, stat_lag: float
) -> None:
    """Wrap the cross-worker READ seams to return stale results with prob ``p_lag``.

    Writes (``os.link``/``os.utime``) stay authoritative, except an optional post-success
    ``link`` error (``p_linkfail``) modelling a retried-RPC that reports failure after
    actually linking -- this exercises the token-confirm path. ``stat_lag`` bounds how
    much older a lagged mtime reads; it is kept ``<< ttl`` (a peer one heartbeat behind),
    matching production where attr-cache lag is small relative to the staleness horizon,
    so a *live* lease is essentially never falsely judged dead.
    """
    real_stat = linklease._stat_mtime
    real_token = linklease._read_token
    real_exists = linklease._exists

    def lag_stat(path: Path) -> float | None:
        m = real_stat(path)
        if m is not None and rnd.random() < p_lag:
            return m - rnd.uniform(0.0, stat_lag)  # one-heartbeat-behind, not "looks dead"
        return m

    def lag_token(path):  # noqa: ANN001,ANN202 -- test shim
        t = real_token(path)
        if t is not None and rnd.random() < p_lag:
            return None  # token not yet visible
        return t

    def lag_exists(path: Path) -> bool:
        e = real_exists(path)
        if e and rnd.random() < p_lag:
            return False  # stale negative (cannot invent a positive -> never returns True falsely)
        return e

    linklease._stat_mtime = lag_stat  # type: ignore[assignment]
    linklease._read_token = lag_token  # type: ignore[assignment]
    linklease._exists = lag_exists  # type: ignore[assignment]

    if p_linkfail > 0.0:
        real_link = os.link

        def flaky_link(src, dst, **kw):  # noqa: ANN001,ANN202 -- test shim
            real_link(src, dst)  # actually performs the link
            if rnd.random() < p_linkfail:
                raise OSError("simulated post-success link RPC error")

        linklease.os.link = flaky_link  # type: ignore[assignment]


# ------------------------------------------------------------------ worker entry
def _worker(args: tuple) -> None:
    (seed, widx, item_ids, out_dir, lease_dir, audit_dir,
     p_lag, p_die, p_linkfail, ttl, hb, die_after) = args
    rnd = random.Random(seed * 100003 + widx)  # int seed (tuple is unsupported)
    _install_read_lag(rnd, p_lag, p_linkfail, stat_lag=hb)  # lag ~ one heartbeat, << ttl

    out = Path(out_dir)
    audit = Path(audit_dir) / f"audit_{os.getpid()}_{widx}.jsonl"
    state = {"processed": 0}

    def is_done(item: str) -> bool:
        exists = (out / f"{item}.json").exists()
        if exists and rnd.random() < p_lag:
            return False  # stale-negative on a freshly-written output
        return exists

    def process(item: str) -> None:
        # Record the attempt FIRST (flushed) so a mid-item death is still audited.
        with open(audit, "a", encoding="ascii") as f:
            f.write(json.dumps({"item": item, "owner": widx, "pid": os.getpid()}) + "\n")
            f.flush()
        time.sleep(rnd.uniform(0.01, 0.05))
        # Simulate walltime death mid-item, while still holding the lease and BEFORE the
        # output exists -> the item must be reclaimed by a peer (stale lease + block-retry).
        if die_after is not None and state["processed"] >= die_after and rnd.random() < p_die:
            os._exit(0)
        atomic_write(out / f"{item}.json", json.dumps({"item": item, "by": widx}))
        state["processed"] += 1

    pool = WorkPool(
        item_ids=item_ids, is_done=is_done, lease_dir=lease_dir,
        ttl=ttl, heartbeat=hb, backoff=(0.05, 0.3),
    )
    pool.run(process)


# --------------------------------------------------- worker entry: poisoned-item fleet
def _poison_worker(args: tuple) -> None:
    """A worker whose pool contains one item that ALWAYS raises a non-retryable error.

    Deliberately a separate entry point from :func:`_worker`: no lag shim, no simulated
    death, no randomness. The property under test is termination of the whole fleet, and
    mixing it with the hammer's chaos would make a hang ambiguous between the two.
    """
    (widx, item_ids, out_dir, lease_dir, poison) = args
    out = Path(out_dir)

    marker = _FileMarker(out)

    def process(item: str) -> None:
        if item == poison:
            raise ValueError("deterministic, non-retryable, every worker, every time")
        atomic_write(out / f"{item}.json", json.dumps({"item": item, "by": widx}))

    def on_error(item: str, exc: Exception) -> None:
        # The realistic shape, and the one that caused the production livelock: a
        # diagnostic sidecar that is NOT the file `is_done` checks.
        atomic_write(out / f"{item}.err.{widx}", f"{type(exc).__name__}: {exc}")

    def on_exhausted(item: str, path: Path) -> None:
        # Idempotent: several workers can legitimately reach this for the same item.
        # Writes to the PATH THE POOL SUPPLIED, not a name of its own devising.
        atomic_write(path, json.dumps({"item": item, "excluded": True}))

    WorkPool(
        item_ids=item_ids, done=marker, lease_dir=lease_dir,
        ttl=5.0, heartbeat=1.0, backoff=(0.05, 0.3),
        # `retryable` is load-bearing here. Left unset, EVERY exception is retryable once
        # max_attempts > 1, so the poison would take the retry path and `on_error` would
        # never fire -- the test would pass while exercising the wrong branch entirely.
        # Naming a type the poison is not puts it on the non-retryable path, which is the
        # one that livelocked.
        max_attempts=2, retryable=Transient, on_exhausted=on_exhausted,
    ).run(process, on_error=on_error)


@pytest.mark.slow
def test_a_poisoned_item_does_not_strand_the_fleet(tmp_path):
    """THE production shape, across real processes -- the one a single-process test cannot show.

    In the incident this fixes, a deterministically-failing item was reclaimed forever. The
    cost was not one stuck worker: once the peers drained everything else they each claimed
    the same poisoned item and blocked too, under block-and-retry termination. A whole fleet
    sat on one item while the run hung just short of complete.

    Every other new test here runs one pool in one process, so each proves only that *a*
    worker terminates. This asserts the fleet property directly: three independent processes,
    one item none of them can ever complete, and all three must exit.

    Uses real process exit codes rather than a thread join, because the failure being guarded
    is an infinite loop -- a hung child must fail the assertion, not silently outlive it.
    """
    out = tmp_path / "out"
    lease_dir = tmp_path / "_pool" / "leases"
    out.mkdir(parents=True)
    lease_dir.mkdir(parents=True)

    items = [f"item_{i:02d}" for i in range(9)]
    poison = items[4]                       # mid-list, so workers reach it at different times

    ctx = mp.get_context("spawn")
    args = [(w, items, str(out), str(lease_dir), poison) for w in range(3)]
    codes = _run_wave(ctx, args, join_timeout=90.0, target=_poison_worker)

    assert codes == [0, 0, 0], (
        f"worker exit codes {codes} -- None means the process was still running at the "
        "timeout, i.e. the fleet livelocked on the poisoned item"
    )
    for item in items:                      # completeness: nothing stranded, poison included
        assert (out / f"{item}.json").exists(), f"{item} never became done"
    assert json.loads((out / f"{poison}.json").read_text())["excluded"] is True
    # on_error still fired, and its sidecar is still not what made the item terminal.
    assert list(out.glob(f"{poison}.err.*")), "on_error never ran for the poisoned item"


# ------------------------------------------------------------------ harness
def _run_wave(ctx, worker_args_list, join_timeout: float, target=None) -> list[int | None]:
    """Spawn one process per arg tuple and join with a timeout.

    ``target`` defaults to the hammer's :func:`_worker`; the poisoned-item test passes its
    own entry point. An exit code of ``None`` means the process was still alive at the
    timeout, which every caller treats as a failure -- that is how a livelock surfaces here
    instead of hanging the suite.
    """
    procs = [ctx.Process(target=target or _worker, args=(a,)) for a in worker_args_list]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=join_timeout)
    codes = [p.exitcode for p in procs]
    for p in procs:  # don't leak a hung process into the next wave
        if p.is_alive():
            p.terminate()
    return codes


def _read_attempts(audit_dir: Path) -> dict[str, int]:
    attempts: dict[str, int] = {}
    for f in audit_dir.glob("audit_*.jsonl"):
        for line in f.read_text(encoding="ascii").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn final line from os._exit mid-write
            attempts[rec["item"]] = attempts.get(rec["item"], 0) + 1
    return attempts


def _one_run(tmp_root: Path, seed: int, *, n_workers: int, n_items: int,
             p_lag: float, p_die: float, p_linkfail: float) -> tuple[int, int]:
    """One 2-wave run. Returns (total_dups, max_dup_for_any_item). Hard-asserts within."""
    run_dir = tmp_root / f"seed_{seed}"
    out_dir = run_dir / "out"
    lease_dir = run_dir / "_pool" / "leases"
    audit_dir = run_dir / "audit"
    for d in (out_dir, lease_dir, audit_dir):
        d.mkdir(parents=True, exist_ok=True)

    items = [f"item_{i:03d}" for i in range(n_items)]
    ttl, hb = 0.8, 0.2
    ctx = mp.get_context("spawn")

    # Wave 1: workers that may die mid-item (leaving stale leases + undone items).
    wave1 = [
        (seed, w, items, str(out_dir), str(lease_dir), str(audit_dir),
         p_lag, p_die, p_linkfail, ttl, hb, 2)
        for w in range(n_workers)
    ]
    codes1 = _run_wave(ctx, wave1, join_timeout=60.0)
    assert all(c is not None for c in codes1), f"a wave-1 worker hung (livelock?): {codes1}"

    # Wave 2: fresh workers (no death), still laggy -> drain remainder + reclaim stale.
    wave2 = [
        (seed + 1000, w, items, str(out_dir), str(lease_dir), str(audit_dir),
         p_lag, 0.0, p_linkfail, ttl, hb, None)
        for w in range(n_workers)
    ]
    codes2 = _run_wave(ctx, wave2, join_timeout=60.0)
    assert all(c is not None for c in codes2), f"a wave-2 worker hung (livelock?): {codes2}"

    # (1) Completeness + (2) single durable record.
    for item in items:
        matches = list(out_dir.glob(f"{item}.json"))
        assert len(matches) == 1, f"{item}: expected exactly one output, got {len(matches)}"
        json.loads(matches[0].read_text(encoding="ascii"))  # parseable -> not torn

    # (3) Bounded duplicates from the attempt audit.
    attempts = _read_attempts(audit_dir)
    assert set(attempts) == set(items), "an item was never attempted (lost item)"
    dups = {it: attempts[it] - 1 for it in items}
    max_dup = max(dups.values())
    total_dups = sum(dups.values())
    # Hard cap catches a ping-pong regression (which would blow far past this); normal
    # lag-induced re-attempts stay well under it across two waves of n_workers each.
    hard_cap = 3 * n_workers + 3
    assert max_dup <= hard_cap, f"unbounded duplication (ping-pong?): max_dup={max_dup} > {hard_cap}"
    return total_dups, max_dup


# ------------------------------------------------------------------ tests
def test_two_workers_complete_all_no_lag(tmp_path):
    """Fast smoke: no lag, no death -> every item done exactly once, all workers exit."""
    total_dups, _ = _one_run(
        tmp_path, seed=0, n_workers=2, n_items=12, p_lag=0.0, p_die=0.0, p_linkfail=0.0
    )
    assert total_dups == 0  # without lag/death there is no reason to double-process


@pytest.mark.slow
def test_exactly_one_completion_under_contention(tmp_path):
    """The hammer: contention + lag + simulated death across a seed ensemble.

    Per-seed hard asserts (completeness, single record, bounded max-dup, liveness) live
    in `_one_run`; here we add the ensemble soft properties: a generous duplicate-RATE
    ceiling and proof that the lag path actually produced at least one duplicate.
    """
    seeds = [1, 2]
    n_workers, n_items = 4, 16
    ensemble_dups = 0
    for s in seeds:
        total_dups, _ = _one_run(
            tmp_path, seed=s, n_workers=n_workers, n_items=n_items,
            p_lag=0.15, p_die=0.5, p_linkfail=0.05,
        )
        # Soft per-seed rate ceiling: total re-processes stay a small multiple of the item
        # count even under aggressive lag + 50%-death. This is a loose sanity heuristic
        # (the per-item hard cap above is the real guard) so it must tolerate platform
        # multiprocessing-scheduling variance: calibrated ~13-14 locally, but ubuntu-3.13
        # CI observed 33, so the bound is 4x the item count -- still orders of magnitude
        # below a genuine ping-pong regression (which the hard cap also catches).
        assert total_dups <= 4 * n_items, f"seed {s}: dup rate too high ({total_dups})"
        ensemble_dups += total_dups
    # Over the ensemble, the lag/contention path MUST have fired at least once (else the
    # test is trivially passing because no concurrency happened).
    assert ensemble_dups >= 1, "no duplicates anywhere -> contention/lag was not exercised"


# ------------------------------------------------------------------ retry-on-failure
# Single-process, deterministic: exercise the max_attempts / on_exhausted / retryable
# surface directly (the cross-process reclaim path is covered by the hammer above).
def _retry_dirs(tmp_path: Path) -> tuple[Path, Path]:
    out = tmp_path / "out"
    lease_dir = tmp_path / "_pool" / "leases"
    out.mkdir(parents=True, exist_ok=True)
    lease_dir.mkdir(parents=True, exist_ok=True)
    return out, lease_dir


def test_max_attempts_gt_one_requires_on_exhausted(tmp_path):
    """Retry (max_attempts > 1) demands on_exhausted; max_attempts < 1 is rejected."""
    _, lease_dir = _retry_dirs(tmp_path)
    with pytest.raises(ValueError, match="on_exhausted"):
        WorkPool(item_ids=["a"], is_done=lambda i: False,
                 lease_dir=str(lease_dir), max_attempts=3)
    with pytest.raises(ValueError, match="max_attempts"):
        WorkPool(item_ids=["a"], is_done=lambda i: False,
                 lease_dir=str(lease_dir), max_attempts=0,
                 on_exhausted=lambda i, p: None)


def test_retry_recovers_across_transient_failures(tmp_path):
    """A transient failure re-runs (same worker here) and succeeds within the budget."""
    out, lease_dir = _retry_dirs(tmp_path)
    items = ["a", "b", "c"]
    calls: dict[str, int] = {}
    exhausted: list[str] = []

    def is_done(i: str) -> bool:
        return (out / f"{i}.json").exists()

    def process(i: str) -> None:
        calls[i] = calls.get(i, 0) + 1
        if i == "b" and calls[i] <= 2:  # fails twice, succeeds on the 3rd attempt
            raise RuntimeError("transient")
        atomic_write(out / f"{i}.json", json.dumps({"item": i}))

    pool = WorkPool(
        item_ids=items, done=_FileMarker(out), lease_dir=str(lease_dir),
        ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=3, on_exhausted=lambda i, p: exhausted.append(i),
    )
    pool.run(process)

    assert all(is_done(i) for i in items)  # all completed, incl. the retried one
    assert calls["b"] == 3                 # 2 failures + 1 success
    assert exhausted == []                 # never gave up
    # 2 failure markers recorded for b (the 3rd attempt succeeded -> no marker).
    assert len(list((tmp_path / "_pool" / "_attempts" / "b").iterdir())) == 2


def test_exhausts_and_terminates_on_permanent_failure(tmp_path):
    """A permanently-failing item exhausts after max_attempts; on_exhausted ends it
    (no livelock: run() returns because on_exhausted makes is_done true)."""
    out, lease_dir = _retry_dirs(tmp_path)
    items = ["a", "b"]
    exhausted: list[str] = []

    def is_done(i: str) -> bool:
        return (out / f"{i}.json").exists()

    def process(i: str) -> None:
        if i == "b":
            raise RuntimeError("permanent")
        atomic_write(out / f"{i}.json", json.dumps({"item": i}))

    def on_exhausted(i: str, path: Path) -> None:
        exhausted.append(i)
        atomic_write(path, json.dumps({"item": i, "excluded": True}))

    pool = WorkPool(
        item_ids=items, done=_FileMarker(out), lease_dir=str(lease_dir),
        ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=2, on_exhausted=on_exhausted,
    )
    pool.run(process)  # must terminate

    assert is_done("a") and is_done("b")  # a succeeded; b terminal via on_exhausted
    assert exhausted == ["b"]             # gave up exactly once
    assert len(list((tmp_path / "_pool" / "_attempts" / "b").iterdir())) == 2  # bounded


class Transient(Exception):
    """An in-scope-for-retry exception, for the `retryable=` tests below."""


class _FileMarker:
    """Minimal `DoneMarker`: one `<item>.json` per item, done iff it exists.

    Its parameter is named `key`, not `item_id`, ON PURPOSE. The protocol declares its
    members positional-only precisely so an implementation can use its own domain's word
    (the real consumer says `stem`), and a helper that happened to match the protocol's
    spelling would leave that untested — the mismatch is the case that breaks under
    parameter-name compatibility checking, so it is the one worth exercising.
    """

    def __init__(self, out: Path) -> None:
        self.out = out

    def exists(self, key: str) -> bool:
        return (self.out / f"{key}.json").exists()

    def new_path(self, key: str) -> Path:
        return self.out / f"{key}.json"


def _run_bounded(pool: WorkPool, process, *, on_error=None, timeout: float = 20.0) -> None:
    """`pool.run(...)` on a worker thread, failing the test if it does not return.

    Every termination test goes through this. The defect these tests guard is a LIVELOCK,
    so the natural regression is an infinite loop -- and a test that regresses by hanging
    tells you nothing and blocks CI. This turns it into an assertion that fails in seconds.
    """
    box: dict[str, BaseException] = {}

    def target() -> None:
        try:
            pool.run(process, on_error=on_error)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread below
            box["exc"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), (
        f"pool.run() did not return within {timeout}s -- the item is being reclaimed "
        "forever (livelock). This is the regression these tests exist to catch."
    )
    if "exc" in box:
        raise box["exc"]


def test_non_retryable_terminates_via_on_exhausted(tmp_path):
    """A non-retryable failure is reported to on_error AND terminated by on_exhausted.

    This test used to be `test_non_retryable_exception_routes_to_on_error`, and it passed
    for the wrong reason: its `on_error` wrote `out/{i}.json`, the exact file `is_done`
    checks, so the item terminated by accident and the livelock never manifested. Deleting
    that one `atomic_write` made it hang forever. It is the only test that ever exercised
    this path, which is why the defect survived.

    The sentinel here is deliberately a NON-done file, mirroring the real consumer
    (an error log beside the output file `is_done` checks): `on_error` is a diagnostic hook,
    and terminality is `on_exhausted`'s job alone.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    errors: list[tuple[str, str]] = []
    exhausted: list[str] = []

    def is_done(i: str) -> bool:
        return (out / f"{i}.json").exists()

    def process(i: str) -> None:
        raise ValueError("fatal, not retryable")

    def on_error(i: str, exc: Exception) -> None:
        errors.append((i, type(exc).__name__))
        atomic_write(out / f"ERROR_{i}.txt", f"{type(exc).__name__}: {exc}")  # NOT is_done

    def on_exhausted(i: str, path: Path) -> None:
        exhausted.append(i)
        atomic_write(path, json.dumps({"item": i, "excluded": True}))

    pool = WorkPool(
        item_ids=["a"], done=_FileMarker(out), lease_dir=str(lease_dir),
        ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=3, on_exhausted=on_exhausted, retryable=Transient,
    )
    _run_bounded(pool, process, on_error=on_error)

    assert errors == [("a", "ValueError")]          # on_error still fires, unchanged
    assert (out / "ERROR_a.txt").exists()           # and its sentinel survives
    assert exhausted == ["a"]                       # exactly one exhaust
    assert is_done("a")                             # terminal, so the pool could exit


def test_non_retryable_gets_exactly_one_attempt(tmp_path):
    """Retrying a non-retryable failure is pointless, so its budget is 1 -- not
    `max_attempts`. Retryable-vs-not decides HOW MANY attempts, never bounded-vs-unbounded."""
    out, lease_dir = _retry_dirs(tmp_path)
    calls: list[str] = []

    def process(i: str) -> None:
        calls.append(i)
        raise ValueError("fatal")

    pool = WorkPool(
        item_ids=["a"], done=_FileMarker(out),
        lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=5, retryable=Transient,
        on_exhausted=lambda i, path: atomic_write(path, "{}"),
    )
    _run_bounded(pool, process, on_error=lambda i, e: None)

    assert calls == ["a"]  # one attempt, despite max_attempts=5
    assert len(list((tmp_path / "_pool" / "_attempts" / "a").iterdir())) == 1


def test_soft_failures_then_hard_failure_terminates(tmp_path):
    """The attempt counter is shared, so a mixed history composes into one budget.

    Two retryable failures then a non-retryable one: the third failure's budget is 1, it is
    already at 3 attempts, so it exhausts rather than consuming the remaining retry.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    seen: list[str] = []

    def process(i: str) -> None:
        seen.append(i)
        raise Transient("soft") if len(seen) <= 2 else ValueError("hard")

    pool = WorkPool(
        item_ids=["a"], done=_FileMarker(out),
        lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=5, retryable=Transient,
        on_exhausted=lambda i, path: atomic_write(path, "{}"),
    )
    _run_bounded(pool, process, on_error=lambda i, e: None)

    assert len(seen) == 3  # 2 soft (retried) + 1 hard (exhausts immediately)


def test_max_attempts_one_with_on_error_still_terminates(tmp_path):
    """`max_attempts=1` is the library default AND the configuration that livelocked.

    With retry off, `_is_retryable` returns False for EVERY exception -- including one the
    caller named in `retryable=` -- so the default configuration routed everything down the
    unterminated path. That is the shape that hung a GPU every ~26s in production.
    """
    out, lease_dir = _retry_dirs(tmp_path)

    def process(i: str) -> None:
        raise Transient("even an in-scope exception is not retryable at max_attempts=1")

    pool = WorkPool(
        item_ids=["a", "b"], done=_FileMarker(out),
        lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=1, retryable=Transient,
        on_exhausted=lambda i, path: atomic_write(path, "{}"),
    )
    _run_bounded(pool, process, on_error=lambda i, e: None)

    assert (out / "a.json").exists() and (out / "b.json").exists()


def test_on_error_without_on_exhausted_is_rejected(tmp_path):
    """Fail closed at startup rather than looping forever.

    `on_error` cannot be validated in `__init__` -- it is a `run()` parameter -- so the
    check lives at the top of `run()`, before any item is claimed. The pairing it rejects
    is precisely what `docs/workpool.md`'s quickstart used to recommend.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    pool = WorkPool(item_ids=["a"], is_done=lambda i: (out / f"{i}.json").exists(),
                    lease_dir=str(lease_dir))

    with pytest.raises(ValueError, match="on_exhausted"):
        pool.run(lambda i: None, on_error=lambda i, e: None)

    assert not any(lease_dir.iterdir())  # rejected before claiming anything


def test_default_config_exception_still_propagates(tmp_path):
    """With neither callback, a hard exception kills the worker exactly as before.

    The terminalization path must not fire when the caller declared no terminal handler:
    calling a `None` `on_exhausted` would raise TypeError from inside an except block and
    mask the real exception, which is strictly worse than the behaviour it replaced.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    pool = WorkPool(item_ids=["a"], is_done=lambda i: (out / f"{i}.json").exists(),
                    lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05))

    def process(i: str) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):   # the ORIGINAL exception, not TypeError
        _run_bounded(pool, process)

    assert not (tmp_path / "_pool" / "_attempts").exists()  # no attempt recorded


def test_exhaust_skipped_when_peer_completed_the_item(tmp_path):
    """A done-marker appearing mid-failure cancels the exhaust.

    The pool permits occasional double-processing by design, so a slow worker can be
    failing on an item a peer has already finished. Both terminal writers in the wild
    OVERWRITE rather than skip -- one to the identical path a success writes -- so
    exhausting here would destroy a real result. The guard is the same TOCTOU re-check
    `_scan_once` already performs after winning a lease.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    exhausted: list[str] = []

    def process(i: str) -> None:
        atomic_write(out / f"{i}.json", json.dumps({"item": i, "real": True}))  # "the peer"
        raise ValueError("crashed after the peer finished")

    pool = WorkPool(
        item_ids=["a"], done=_FileMarker(out),
        lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=2, retryable=Transient,
        on_exhausted=lambda i, path: exhausted.append(i),
    )
    _run_bounded(pool, process, on_error=lambda i, e: None)

    assert exhausted == []                                             # never called
    assert json.loads((out / "a.json").read_text())["real"] is True    # result intact


def test_on_exhausted_that_never_terminates_is_surfaced(tmp_path):
    """A terminal handler that does not make `is_done` true is caught, not trusted.

    Tolerant once -- NFS attribute-cache lag produces stale negatives and the module's
    whole termination proof rests on those being safe. But the confirmation marker is
    durable and fleet-wide, not per-process: these workers join late and die on walltime,
    so a per-process set would warn once per process forever and never escalate. Two pool
    instances here stand in for two workers sharing the filesystem.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    logs: list[str] = []
    done = {"a": False}

    def process(i: str) -> None:
        raise ValueError("boom")

    class _DictMarker:
        """A marker whose done-state is a dict the test drives directly.

        `new_path` still returns a real path, and the handlers below deliberately ignore
        it — which is the point: supplying the path makes the RIGHT write easy, it cannot
        force a handler to perform one. That residual is exactly what `_verify_terminal`
        exists to catch, so this test drives the seam and the backstop together.
        """

        def exists(self, key: str) -> bool:
            return done[key]

        def new_path(self, key: str) -> Path:
            return out / f"{key}.json"

    def make_pool(on_exhausted) -> WorkPool:
        return WorkPool(
            item_ids=["a"], done=_DictMarker(),
            lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
            log=logs.append, max_attempts=1, on_exhausted=on_exhausted,
        )

    # First worker: on_exhausted writes nothing the first time (the bug), so verification
    # fails and drops one marker. It relents on the second exhaust purely so this worker
    # can finish -- a genuinely broken handler would loop, which is the designed tolerance
    # and not what is under test here.
    exhausts: list[str] = []

    def relents_on_second(i: str, path: Path) -> None:
        exhausts.append(i)
        if len(exhausts) >= 2:
            done[i] = True

    _run_bounded(make_pool(relents_on_second), process, on_error=lambda i, e: None)

    markers = [p for p in (tmp_path / "_pool" / "_attempts" / "a").iterdir()
               if p.name.startswith("exhausted_")]
    assert len(markers) == 1                                    # warned once, wrote once
    assert any("is_done still false" in m for m in logs)        # and said so
    assert not any("MUST write" in m for m in logs)             # but did not raise

    # A second worker on the same filesystem -- a fresh process in production -- finds the
    # first worker's marker and escalates. A per-process set could not do this: these
    # workers die on walltime, so each new one would warn once and exit forever.
    done["a"] = False
    with pytest.raises(RuntimeError, match="MUST write the durable output"):
        _run_bounded(make_pool(lambda i, path: None), process, on_error=lambda i, e: None)


# =======================================================================================
# The done-marker SEAM.
#
# The livelock these guard is not "no handler ran" (that is bounded already) but "the
# handler ran and wrote a name `is_done` does not read". It has happened: a terminal
# marker written under one filename token while the done-check globbed another -- the
# right handler, the right kind of file, the wrong name, reclaimed forever.
#
# `is_done` alone can never prevent it, because the pool never learns the name. A marker
# owns both halves, so the pool can hand the handler the path it must write.
# =======================================================================================


def test_a_terminal_handler_without_a_marker_is_rejected(tmp_path):
    """Fail closed at construction rather than let a handler name its own output."""
    out, lease_dir = _retry_dirs(tmp_path)

    with pytest.raises(ValueError, match="on_exhausted requires done="):
        WorkPool(item_ids=["a"], is_done=lambda i: (out / f"{i}.json").exists(),
                 lease_dir=str(lease_dir), on_exhausted=lambda i, p: None)


def test_exactly_one_of_is_done_or_done_is_required(tmp_path):
    """Two answers to "is it done" is the shape this parameter exists to remove."""
    _, lease_dir = _retry_dirs(tmp_path)

    with pytest.raises(ValueError, match="exactly one"):
        WorkPool(item_ids=["a"], lease_dir=str(lease_dir))
    with pytest.raises(ValueError, match="exactly one"):
        WorkPool(item_ids=["a"], lease_dir=str(lease_dir),
                 is_done=lambda i: False, done=_FileMarker(Path(tmp_path)))


def test_on_exhausted_is_handed_a_path_that_satisfies_the_done_check(tmp_path):
    """The seam itself: the handler does not compute a name, it is given one.

    Asserted against the MARKER rather than a hand-spelled filename -- spelling one here
    would re-create at test level the very second derivation the change removes.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    marker = _FileMarker(out)
    seen: list[Path] = []

    def on_exhausted(item: str, path: Path) -> None:
        seen.append(path)
        atomic_write(path, "{}")

    _run_bounded(
        WorkPool(item_ids=["a"], done=marker, lease_dir=str(lease_dir),
                 ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
                 on_exhausted=on_exhausted),
        lambda i: (_ for _ in ()).throw(ValueError("boom")),
        on_error=lambda i, e: None,
    )

    assert seen == [marker.new_path("a")]
    assert marker.exists("a")


def test_a_marker_whose_halves_disagree_is_caught_not_looped(tmp_path):
    """The production incident, reproduced through the seam.

    A marker that mints one name and checks another is the residual the seam cannot rule
    out -- supplying a path makes the right write easy, it cannot force one. What it must
    NOT do is loop: `_verify_terminal` escalates on the second durable marker instead.
    """
    out, lease_dir = _retry_dirs(tmp_path)

    class _DisagreeingMarker:
        """Mints `<item>.new` but checks `<item>.json` -- the serial-livelock shape."""

        def exists(self, key: str) -> bool:
            return (out / f"{key}.json").exists()

        def new_path(self, key: str) -> Path:
            return out / f"{key}.new"

    def process(i: str) -> None:
        raise ValueError("boom")

    logs: list[str] = []
    pool = WorkPool(item_ids=["a"], done=_DisagreeingMarker(),
                    lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0,
                    backoff=(0.01, 0.05), max_attempts=1, log=logs.append,
                    on_exhausted=lambda i, path: atomic_write(path, "{}"))

    # ONE worker suffices, and that is the geometry again: the item stays undone and its
    # lease is released, so the same worker re-claims it, exhausts a second time, and
    # trips the durable escalation within seconds. Tolerance is still real -- the first
    # exhaust only warns -- it is just spent fast rather than across the fleet.
    with pytest.raises(RuntimeError, match="MUST write the durable output"):
        _run_bounded(pool, process, on_error=lambda i, e: None, timeout=20.0)

    assert any("is_done still false" in m for m in logs)     # warned before it raised
    assert (out / "a.new").exists()                          # it DID write, just not where
    assert not (out / "a.json").exists()


# --------------------------------------------------------------------------- #
# the THIRD writer: `process` itself
# --------------------------------------------------------------------------- #

def test_a_process_that_never_completes_its_item_raises_instead_of_looping(tmp_path):
    """`process` is the only writer whose naming bug used to loop forever undetected.

    It raises nothing, so no attempt is recorded, no budget spends, `on_exhausted` never
    fires and `_verify_terminal` never runs. The item is simply reclaimed, at a full unit
    of real work per cycle, with nothing in the pool noticing.

    Note what this test asserts about the LOOP GEOMETRY, because the obvious spec is
    wrong: `_scan_once` walks a fixed order and takes the first undone item, and the
    failing item's lease was just released -- so the worker re-claims THE SAME item and
    never reaches a second. A counter keyed on distinct items could never leave 1.

    WHICH item it pins on is not predictable (`_start_offset` is derived from a per-process
    uuid), so this asserts the shape -- exactly one item, whichever it is -- rather than a
    name. Asserting `item_00` passes or fails by luck of the rotation.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    touched: list[str] = []

    def process(item: str) -> None:
        touched.append(item)
        atomic_write(out / f"{item}.WRONG", "{}")     # never what the marker checks

    with pytest.raises(RuntimeError, match="without is_done becoming true"):
        _run_bounded(
            WorkPool(item_ids=[f"item_{i:02d}" for i in range(4)], done=_FileMarker(out),
                     lease_dir=str(lease_dir), ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05)),
            process,
        )

    assert len(set(touched)) == 1, (
        f"expected the worker to pin on ONE item, saw {sorted(set(touched))} -- if this "
        "ever spreads across items, the consecutive-miss counter needs rethinking"
    )
    assert len(touched) == 3, f"expected exactly the miss limit, saw {len(touched)}"


def test_one_lagged_done_check_is_tolerated_and_resets(tmp_path):
    """A stale-negative `is_done` costs a re-process, not a raise.

    The module's termination proof rests on stale negatives being safe, so the counter has
    to distinguish "the filesystem is behind" from "your names disagree". One miss then a
    success must leave no residue -- otherwise lag on a long run would eventually trip it.
    """
    out, lease_dir = _retry_dirs(tmp_path)
    logs: list[str] = []
    lag = {"pending": True}

    class _LaggyMarker:
        def exists(self, key: str) -> bool:
            if lag["pending"] and (out / f"{key}.json").exists():
                lag["pending"] = False        # one stale negative, then the truth
                return False
            return (out / f"{key}.json").exists()

        def new_path(self, key: str) -> Path:
            return out / f"{key}.json"

    _run_bounded(
        WorkPool(item_ids=["a", "b", "c"], done=_LaggyMarker(), lease_dir=str(lease_dir),
                 ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05), log=logs.append),
        lambda i: atomic_write(out / f"{i}.json", "{}"),
    )

    assert any("processed but is_done still false" in m for m in logs)   # warned once
    assert all((out / f"{i}.json").exists() for i in ("a", "b", "c"))    # and finished
