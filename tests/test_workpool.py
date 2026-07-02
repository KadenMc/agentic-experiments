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
lease) to exercise stale-reclaim + block-and-retry. Mirrors the spawn structure of
electricrag's `tests/test_probe_ledger.py`.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import random
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


# ------------------------------------------------------------------ harness
def _run_wave(ctx, worker_args_list, join_timeout: float) -> list[int | None]:
    procs = [ctx.Process(target=_worker, args=(a,)) for a in worker_args_list]
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
                 on_exhausted=lambda i: None)


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
        item_ids=items, is_done=is_done, lease_dir=str(lease_dir),
        ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=3, on_exhausted=exhausted.append,
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

    def on_exhausted(i: str) -> None:
        exhausted.append(i)
        atomic_write(out / f"{i}.json", json.dumps({"item": i, "excluded": True}))

    pool = WorkPool(
        item_ids=items, is_done=is_done, lease_dir=str(lease_dir),
        ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=2, on_exhausted=on_exhausted,
    )
    pool.run(process)  # must terminate

    assert is_done("a") and is_done("b")  # a succeeded; b terminal via on_exhausted
    assert exhausted == ["b"]             # gave up exactly once
    assert len(list((tmp_path / "_pool" / "_attempts" / "b").iterdir())) == 2  # bounded


def test_non_retryable_exception_routes_to_on_error(tmp_path):
    """With `retryable` set, an out-of-scope exception goes to on_error, not the retry
    path (attempt count untouched)."""
    out, lease_dir = _retry_dirs(tmp_path)

    class Transient(Exception):
        pass

    errors: list[tuple[str, str]] = []

    def is_done(i: str) -> bool:
        return (out / f"{i}.json").exists()

    def process(i: str) -> None:
        raise ValueError("fatal, not retryable")

    def on_error(i: str, exc: Exception) -> None:
        errors.append((i, type(exc).__name__))
        atomic_write(out / f"{i}.json", json.dumps({"item": i, "errored": True}))

    pool = WorkPool(
        item_ids=["a"], is_done=is_done, lease_dir=str(lease_dir),
        ttl=5.0, heartbeat=1.0, backoff=(0.01, 0.05),
        max_attempts=3, on_exhausted=lambda i: None, retryable=Transient,
    )
    pool.run(process, on_error=on_error)

    assert errors == [("a", "ValueError")]                      # routed to on_error
    assert not (tmp_path / "_pool" / "_attempts").exists()      # retry path never touched
