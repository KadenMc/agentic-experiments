# Work-stealing pool (`aexp.workpool`)

`WorkPool` lets several **already-running, independent** worker processes
cooperatively drain *one* body of work over a **shared filesystem** — with no
daemon, broker, or scheduler, and with workers free to join late or die mid-run.
It is opt-in (`from aexp.workpool import WorkPool`) and is not imported at `aexp`
package init, so it costs nothing if you never use it.

**When to reach for it.** Only when a real scheduler doesn't fit. If `sbatch
--array`, a job queue, GNU parallel from a single master, or redis/celery/dask
works for you, use that instead. `aexp.workpool` is for the niche where none of
them apply — because the workers are launched independently (interactively, or by
an agent) and there is no broker process you're allowed to run. Most commonly:
agent-driven GPU jobs on a shared-filesystem HPC.

## What it does

Replaces *static sharding* ("here is worker `k`'s pre-assigned list") with a
*claimable pool* ("any worker takes the next undone item"). Each item is claimed with
an NFS-safe [`LinkLease`](../src/aexp/utils/linklease.py) (an `os.link`-based lease —
atomic across NFS versions where `flock`/`O_EXCL` are not). A dead worker's claim goes
stale and is reclaimed by a peer. Consequences: an idle worker joins instantly (no
repartition), heterogeneous workers don't tail-idle, a dead worker's item returns to the
pool automatically, and the *duplicate-shard* bug class dissolves (there are no static
assignments to overlap).

```
        shared filesystem
        ┌───────────────────────────────────────────┐
        │  out_dir/_pool/leases/<item>.lease  (claims)│
        │  out_dir/<item>.<ext>               (done)  │
        └───────────────────────────────────────────┘
            ▲            ▲            ▲
        worker A     worker B     worker C        each: claim_next -> process -> release
        (GPU 1)      (GPU 2)      (joined late)   a dead worker's lease is reclaimed
```

## The correctness model — read this

**The lease is an efficiency optimization, not the correctness mechanism.** Correctness
comes from *your* `process` writing its output **atomically** (so a killed worker never
leaves a torn file) and, where a shared tally exists, an **idempotent** record. Occasional
double-*processing* of one item (under NFS attribute-cache lag or a falsely-broken stale
lease) is explicitly **acceptable and safe** — the lease only makes it rare. The pool
guarantees **completeness and liveness**, *not* zero-duplicate processing. Do not build a
caller that breaks if an item is processed twice.

Two invariants the type signature can't express:

1. **`is_done(item)` must be monotonic** — once true it stays true — and become true only
   as a **durable effect of a completed `process(item)`** (canonically: an atomically
   written output file exists). This is what makes block-and-retry termination safe. An
   `is_done` that can flip back to false (a lock, a rolled-back row, a cleaned temp)
   breaks termination.
2. **`item_id` must be a filesystem-safe basename** (no `/` or `\`, not `.`/`..`) — it
   names a lease file.

## Minimal usage

Run the **same** script on every worker, over the **same** `item_ids` and `lease_dir` on
the shared filesystem:

```python
from pathlib import Path
from aexp.workpool import WorkPool, probe_exclusive_create
from aexp.utils.atomic import atomic_write

OUT = Path("/shared/run42/out")
items = [f"item_{i:04d}" for i in range(512)]          # the full universe; every worker passes it

def is_done(item: str) -> bool:                         # monotonic; output existence
    return (OUT / f"{item}.json").exists()

def process(item: str) -> None:                         # your work; write output ATOMICALLY
    result = do_expensive_thing(item)
    atomic_write(OUT / f"{item}.json", result)          # this file IS the done-marker

probe_exclusive_create(OUT)                             # fail-closed: refuse to start on a bad FS
WorkPool(item_ids=items, is_done=is_done, lease_dir=OUT / "_pool" / "leases").run(process)
```

`run(process, on_error=...)` owns the load-bearing protocol — claim, release in a
`finally` (a failing item is reclaimed, not stranded), heartbeat the active lease, and
exit only when **every** item is done. Pass `on_error=lambda item, exc: ...` so one
failing item doesn't kill the sweep. Advanced callers can drive `claim_next()` /
`mark_done()` manually (use the pool as a context manager so the heartbeat thread stops).

### Tuning

- `ttl` (default 600 s) is how fast a **dead** worker's item is reclaimed. A live worker
  keeps its lease fresh via the heartbeat, so `ttl` is **decoupled from item duration** —
  it need only exceed a few heartbeat periods, not your longest item.
- `heartbeat` defaults to `ttl/5` (tolerate ~4 missed beats before a peer judges a lease
  stale). Set both larger if your items hold the GIL for long C calls.

### Retry on failure (`max_attempts`)

By default (`max_attempts=1`) a `process` exception routes straight to `on_error` and the
item is left as today — unchanged behavior. Set `max_attempts > 1` to **bound-retry** a
failing item instead:

- A retryable exception writes **no output**, so `is_done` stays false and the item is
  reclaimed and re-run — **by any worker**, so retry spans the fleet (a heavy item that
  OOMs a small GPU can be re-run on a bigger one). Attempts are counted durably on the
  shared filesystem (`_pool/_attempts/<item>/`), so the bound holds across workers.
- After `max_attempts` failures the pool calls **`on_exhausted(item)`** — **required when
  `max_attempts > 1`**. It MUST make `is_done(item)` true durably (e.g. write an
  excluded/void output); that is what stops the item being reclaimed forever and lets the
  pool terminate — the same role a successful output plays. Make it idempotent (a rare
  double-exhaust under lag must be safe), like `process`.
- `retryable=<ExcType>` (or a tuple of types) restricts which exceptions bound-retry;
  anything else falls through to `on_error` as usual. Default `None` = all `Exception`s.
- Worker **death** (no exception — walltime/VPN) is orthogonal: always reclaimed via the
  stale lease, never counted as an attempt.

```python
WorkPool(
    item_ids=items, is_done=is_done, lease_dir=OUT / "_pool" / "leases",
    max_attempts=3,
    on_exhausted=lambda item: atomic_write(OUT / f"{item}.json", EXCLUDED),  # makes is_done true
).run(process)
```

## `workpool` vs `aexp.queue` — which one?

They are **orthogonal and compose**; pick by granularity.

| | `aexp.queue` | `aexp.workpool` |
|---|---|---|
| Granularity | coarse — whole **runs** | fine — **items within one run** |
| Drivers | one process iterates pending runs | many independent workers steal items |
| About | run **provenance + registration** (signac) | **liveness / reclaim / contention** on a shared FS |
| Concurrency model | none | the whole point (leases, stale-reclaim) |

Use `aexp.queue` to register and materialize N tracked runs on one machine. Use
`aexp.workpool` *inside* a run to fan its items across many already-running workers. A
single queued run can use a `WorkPool` internally; they don't compete.

## What is and isn't proven where

- The cross-process **exactly-one-completion under contention + simulated attribute-cache
  lag** property is proven by `tests/test_workpool.py` (an N-process spawn hammer) — on a
  local filesystem, which validates the *algorithm*, not real NFS semantics.
- `probe_exclusive_create(run_dir)` is a fail-closed startup check: it proves the
  filesystem can do atomic exclusive link-create *at all* (catches a grossly misconfigured
  mount). It **cannot** prove cross-node server-side exclusivity from one process — run a
  small multi-worker smoke on your actual cluster before a large campaign.
