"""Unit tests for the NFS-safe link()-lease primitive (`aexp.utils.linklease`).

Covers acquire / EEXIST / idempotent re-acquire, stale-lease reclaim, the
slow-but-alive (refreshed) lease that must NOT be stolen, token-checked
release/refresh (a stolen lease is never deleted or re-taken), candidate sweep, and
the fail-closed `probe_exclusive_create` startup guard (both directions of failure
simulated).
"""
from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from aexp.utils import linklease
from aexp.utils.linklease import LinkLease, LinkLeaseUnsupported, probe_exclusive_create


def test_acquire_then_blocked_by_live_peer(tmp_path):
    a = LinkLease(tmp_path, owner_id="a", ttl=600)
    b = LinkLease(tmp_path, owner_id="b", ttl=600)
    assert a.acquire("item")
    assert a.held_by_me("item")
    assert not b.acquire("item")  # a holds a fresh lease -> b loses
    assert not b.held_by_me("item")


def test_acquire_is_idempotent_for_owner(tmp_path):
    a = LinkLease(tmp_path, owner_id="a")
    assert a.acquire("item")
    assert a.acquire("item")  # re-acquiring our own lease is a no-op True
    assert a.held_by_me("item")


def test_acquire_reclaims_stale_lease(tmp_path):
    dead = LinkLease(tmp_path, owner_id="dead", ttl=0.5)
    assert dead.acquire("item")
    lease = dead._lease_path("item")
    old = time.time() - 100  # age the lease well past ttl
    os.utime(lease, (old, old))

    live = LinkLease(tmp_path, owner_id="live", ttl=0.5)
    assert live.acquire("item")  # breaks the dead lease, then wins via os.link
    assert live.held_by_me("item")
    assert not dead.held_by_me("item")


def test_break_stale_aborts_if_lease_was_refreshed(tmp_path):
    # A lease we *observed* as old must NOT be broken if it has since been refreshed
    # (slow-but-alive worker). _break_stale re-reads and aborts on any mtime change.
    a = LinkLease(tmp_path, owner_id="a", ttl=0.5)
    assert a.acquire("item")
    lease = a._lease_path("item")
    tok = linklease._read_token(lease)
    stale_observation = lease.stat().st_mtime - 100  # pretend we saw it old
    assert a._break_stale(lease, observed_token=tok, observed_mtime=stale_observation) is False
    assert lease.exists()  # untouched


def test_release_is_token_checked(tmp_path):
    # A worker whose lease was reclaimed+retaken by a peer must not delete the peer's lease.
    a = LinkLease(tmp_path, owner_id="a")
    b = LinkLease(tmp_path, owner_id="b")
    assert a.acquire("item")
    a.release("item")
    assert b.acquire("item")  # b now owns it
    a.release("item")  # a is no longer owner -> compare-and-delete must skip
    assert b.held_by_me("item")


def test_refresh_does_not_reacquire_a_stolen_lease(tmp_path):
    a = LinkLease(tmp_path, owner_id="a")
    b = LinkLease(tmp_path, owner_id="b")
    assert a.acquire("item")
    a.release("item")
    assert b.acquire("item")  # b owns it now
    a.refresh("item")  # a no longer owns -> must NOT re-take (avoids ping-pong)
    assert b.held_by_me("item")
    assert not a.held_by_me("item")


def test_refresh_bumps_mtime_when_owned(tmp_path):
    a = LinkLease(tmp_path, owner_id="a")
    assert a.acquire("item")
    lease = a._lease_path("item")
    old = time.time() - 50
    os.utime(lease, (old, old))
    a.refresh("item")
    assert lease.stat().st_mtime > old + 1  # mtime advanced toward now


def test_sweep_candidates_removes_only_stale_temps(tmp_path):
    a = LinkLease(tmp_path, owner_id="a", ttl=0.5)
    stale = tmp_path / ".item.a.deadbeef.tmp"
    stale.write_text("x", encoding="ascii")
    old = time.time() - 100
    os.utime(stale, (old, old))
    fresh = tmp_path / ".item.a.freshone.tmp"
    fresh.write_text("y", encoding="ascii")

    assert a.sweep_candidates() == 1
    assert not stale.exists()
    assert fresh.exists()  # a peer may be mid-acquire -> left alone


def test_probe_exclusive_create_passes_on_real_fs(tmp_path):
    # NTFS/ext4 both support atomic exclusive link-create -> no raise.
    probe_exclusive_create(tmp_path)


def test_probe_rejects_fs_without_hardlinks(tmp_path, monkeypatch):
    import shutil

    def fake_link(src, dst):  # a "link" that copies -> st_nlink stays 1
        shutil.copyfile(src, dst)

    monkeypatch.setattr(linklease.os, "link", fake_link)
    with pytest.raises(LinkLeaseUnsupported):
        probe_exclusive_create(tmp_path)


def _probe_worker(barrier, run_dir: str, iterations: int, err_dir: str) -> None:
    """Spawn target: hammer ``probe_exclusive_create`` on one shared run_dir."""
    barrier.wait(timeout=30)
    try:
        for _ in range(iterations):
            probe_exclusive_create(run_dir)
    except LinkLeaseUnsupported as exc:
        Path(err_dir, f"err_{os.getpid()}.txt").write_text(str(exc), encoding="ascii")
        raise  # non-zero exit code signals the false failure to the parent


def test_probe_is_safe_under_concurrent_startup(tmp_path):
    """N processes probing the SAME run_dir concurrently must all pass.

    This is the normal WorkPool fleet-startup path, not an edge case: every worker
    calls the probe on the shared run_dir at launch. A probe whose link-create
    self-test touches any shared path collides with its peers (a peer's in-flight
    target makes ``os.link`` raise ``FileExistsError``, or a peer's cleanup deletes
    ours mid-test) and gets misreported as ``LinkLeaseUnsupported`` on a perfectly
    healthy filesystem.
    """
    n_procs, iterations = 8, 25
    err_dir = tmp_path / "errs"
    err_dir.mkdir()
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(n_procs)  # maximize overlap: all start probing together
    procs = [
        ctx.Process(
            target=_probe_worker, args=(barrier, str(tmp_path), iterations, str(err_dir))
        )
        for _ in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    codes = [p.exitcode for p in procs]
    for p in procs:  # don't leak a hung process
        if p.is_alive():
            p.terminate()
    errors = [f.read_text(encoding="ascii") for f in err_dir.glob("err_*.txt")]
    assert codes == [0] * n_procs, f"exit codes {codes}; false failures: {errors}"


def test_probe_rejects_fs_without_exclusive_create(tmp_path, monkeypatch):
    real_link = os.link

    def fake_link(src, dst):  # clobbers instead of raising EEXIST -> exclusivity unenforced
        if os.path.exists(dst):
            os.remove(dst)
        real_link(src, dst)

    monkeypatch.setattr(linklease.os, "link", fake_link)
    with pytest.raises(LinkLeaseUnsupported):
        probe_exclusive_create(tmp_path)
