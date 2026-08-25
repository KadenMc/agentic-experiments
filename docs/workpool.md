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

Three invariants the type signature can't express:

1. **`is_done(item)` must be monotonic** — once true it stays true — and become true only
   as a **durable effect of a completed `process(item)`, or of `on_exhausted(item)`**
   (canonically: an atomically written output file exists). This is what makes
   block-and-retry termination safe. An `is_done` that can flip back to false (a lock, a
   rolled-back row, a cleaned temp) breaks termination.
2. **`item_id` must be a filesystem-safe basename** (no `/` or `\`, not `.`/`..`) — it
   names a lease file.
3. **`on_exhausted` is the only terminal handler**, and with `done=` the pool gives it the
   path to write. Every item that stops being worked on goes through it. `on_error` is a
   *diagnostic* hook — whatever it writes, the pool still exhausts the item. Writing a
   sidecar from `on_error` and expecting the pool to move on is the mistake this module
   used to permit silently: the sidecar isn't what `is_done` checks, so the item was
   reclaimed forever.

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
exit only when **every** item is done. Advanced callers can drive `claim_next()` /
`mark_done()` manually (use the pool as a context manager so the heartbeat thread stops).

> **A failing item is not finished by `on_error`.** Because the pool exits only when every
> item is done, the *only* thing that lets it move past a failure is `on_exhausted` writing
> the durable output `is_done` checks. `on_error` is a diagnostic hook — a place to record
> what went wrong — and an error sentinel written there is, by construction, not the
> done-marker. So `run()` **rejects** an `on_error` without an `on_exhausted`: that pairing
> looks like "don't let one bad item kill the sweep" and behaves like an infinite reclaim.
> See [Retry on failure](#retry-on-failure-max_attempts) for the full lifecycle.

### Tuning

- `ttl` (default 600 s) is how fast a **dead** worker's item is reclaimed. A live worker
  keeps its lease fresh via the heartbeat, so `ttl` is **decoupled from item duration** —
  it need only exceed a few heartbeat periods, not your longest item.
- `heartbeat` defaults to `ttl/5` (tolerate ~4 missed beats before a peer judges a lease
  stale). Set both larger if your items hold the GIL for long C calls.

### Retry on failure (`max_attempts`)

**Every failure gets a budget and a terminal state.** `max_attempts` sets how many attempts
a *retryable* failure gets; it does not decide whether a failure terminates. A
non-retryable failure has a budget of 1 — retrying it is pointless — but a budget of 1 is
still a budget, and it still ends in `on_exhausted`.

- A retryable exception writes **no output**, so `is_done` stays false and the item is
  reclaimed and re-run — **by any worker**, so retry spans the fleet (a heavy item that
  OOMs a small GPU can be re-run on a bigger one). Attempts are counted durably on the
  shared filesystem (`_pool/_attempts/<item>/`), so the bound holds across workers.
- Once an item spends its budget the pool calls **`on_exhausted(item, path)`**, where
  `path` comes from `done.new_path(item)`. Write the terminal record **there**: that is what
  stops the item being reclaimed forever and lets the pool terminate — the same role a
  successful output plays. Make it idempotent (a rare double-exhaust under lag must be
  safe), like `process`. Requires `done=`; itself required when `max_attempts > 1`, and
  whenever you pass `on_error`.
- The pool **verifies** that promise rather than trusting it. If `is_done` is still false
  after `on_exhausted`, it warns and records a durable marker; a second such marker
  *anywhere in the fleet* raises. One stale-negative read is tolerated (NFS lag); a
  genuinely wrong marker name — the classic version of this bug — is not.
- **`process` is checked too.** A `process` that returns successfully without making
  `is_done` true would otherwise be reclaimed forever with nothing raised — it throws no
  exception, so no attempt is recorded and `on_exhausted` never runs. The pool warns on a
  miss and raises after three **consecutive** misses. Consecutive on *any* item, not on
  distinct ones: a worker whose writes land under the wrong name re-claims the same item
  every cycle and never reaches a second, so a distinct-item counter could never escalate.
- If a peer completed the item while you were failing, the exhaust is **skipped**. The pool
  permits occasional double-processing, and a terminal writer run over a finished item
  would overwrite a real result.
- `retryable=<ExcType>` (or a tuple of types) restricts which exceptions get *retried*;
  an out-of-scope exception is still reported to `on_error` and still terminates through
  `on_exhausted`, it just gets one attempt. Default `None` = all `Exception`s.
- Worker **death** (no exception — walltime/VPN) is orthogonal: always reclaimed via the
  stale lease, never counted as an attempt.

### The marker (`done=`)

Once you have a terminal handler, pass a **`DoneMarker`** instead of a bare `is_done` — an
object owning both the done-check and the output's *name*:

```python
class Outputs:                                   # your naming, in one place
    def exists(self, item):   return (OUT / f"{item}.json").exists()
    def new_path(self, item): return OUT / f"{item}.json"

WorkPool(
    item_ids=items, done=Outputs(), lease_dir=OUT / "_pool" / "leases",
    max_attempts=3,
    on_exhausted=lambda item, path: atomic_write(path, EXCLUDED),   # writes where exists() looks
).run(process)
```

`run()` **rejects** `on_exhausted` without `done=`, because the alternative is the handler
naming its own file. That is not hypothetical: a terminal marker written under one filename
token while the done-check globbed another cost a production run — the right handler, the
right kind of file, the wrong name, and an item reclaimed forever. Handing the handler
`done.new_path(item)` makes the two agree by construction.

`new_path`, not `path`: if your names carry a timestamp or other per-write component,
`exists` is a glob and each write mints a fresh name. Returning a new path each call serves
both shapes — and if you resolve duplicates by recency, it is what keeps a terminal marker
sorting *after* the run's real output instead of colliding with it.

The bare `is_done=` form remains for callers with no terminal handler, like the quickstart
above. Marker methods are matched structurally and positionally, so name the parameter
whatever your domain calls it.

**Heterogeneous-pool caveat.** Retry recovers a failure only if a *later* attempt can
succeed. When failures are **capacity-bound** — an item too big for a small worker, so it
fails *deterministically* on that worker — a heterogeneous pool can *false-exhaust* the item:
undersized workers burn its attempts before a bigger one claims it. Keep the bound uniform
(do **not** special-case it by worker size — that pushes root-cause awareness into the
primitive); handle this operationally (size the pool so the work fits everywhere) or, as a
future general extension, by capacity-aware routing (prefer re-running an item on a worker
advertising more free memory — which stays uniform: an item that already exhausted the
biggest worker correctly gives up).

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
