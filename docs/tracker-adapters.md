# Tracker bindings

`agentic-experiments` offers four ways to link a signac run to a tracker
(wandb or noop). They're all first-class; pick based on *where* the run
actually executes and *who* owns `wandb.init`:

| Mode | You own | aexp owns | Use when |
|---|---|---|---|
| **Managed** — `tracked_run` | nothing | init + bind + finish | Training runs in the same Python process as aexp. Simplest flow. |
| **Bring your own init** — `prepare_tracker` + `ctx.bind(run)` | `wandb.init` and `run.finish` | the disciplined payload (group / tags / config / notes / dir / mode) and the signac binding | You already have a `wandb.init` call you want to keep (per-item short runs with custom names, `wandb.Table` uploads, memory instrumentation). |
| **CLI adapter bind** — `aexp bind-tracker <job_id> --backend wandb` | nothing | everything, via the `WandbAdapter` | Training runs in a subprocess / separate script / cluster job where reaching back to Python isn't convenient. The CLI stamps the binding; your training code runs in the workspace independently. |
| **Noop / custom adapter** — `bind_tracker(job, NoopAdapter(), ...)` | nothing | everything, through the `TrackerAdapter` ABC | No wandb account; tests; backend-agnostic code; custom backends. |

None of these is deprecated. The Python paths (`tracked_run`,
`prepare_tracker`) and the CLI adapter path (`aexp bind-tracker`) are
complementary — the choice is who controls the `wandb.init` call site.
Once a run is initialized (by any mode), the yielded / bound `wandb.Run`
exposes the full wandb API — `run.log_artifact`, `wandb.Table`,
`run.define_metric`, `run.summary[...]`, `run.alert(...)`, sweeps — none
of it is hidden behind aexp.

## 1. Managed runs — `tracked_run`

Start here if you're writing new code and aexp is the only caller of
`wandb.init` in your stack.

```python
from aexp import create_run, tracked_run
import wandb

job = create_run(
    experiment_id="E018",
    hypothesis_id="H012",
    statepoint={"condition": "full", "seed": 0},
)

with tracked_run(job, project="my-project", offline=True) as run:
    # `run` is a real wandb.Run — full API is available.
    run.log({"loss": 0.12, "acc": 0.93})
    run.log_artifact(wandb.Artifact("preds", type="eval"))
    run.summary["final_acc"] = 0.93
    run.define_metric("epoch")
# On exit: aexp called run.finish(exit_code=0) — or exit_code=1 on exception.
```

What `tracked_run` does:

- Derives the deterministic group slug `H###/E###/condition` from the linked
  research artifacts and `job.sp`.
- Assembles tags (`kind=experiment`, `H###`, `E###`, `condition=X`), pulls
  the hypothesis statement / local hypothesis / success criteria into
  `notes`, flattens the state point into `config`, and sets `dir` to the
  signac job workspace so offline-run data co-locates with the job.
- Calls `wandb.init(**init_kwargs)` exactly once per `with` block.
- Stamps `job.doc["tracker"] = {backend, run_id, url, project, group}`.
- Calls `run.finish(exit_code=...)` on exit.

`tracked_run` does NOT manage signac status transitions — compose
`aexp.run_lifecycle` alongside if you want both:

```python
from aexp import run_lifecycle, tracked_run

with run_lifecycle(job), tracked_run(job, project="my-project") as run:
    ...
```

### Extra kwargs

Caller-owned: `name`, `job_type`, plus any `**wandb_kwargs` you pass (e.g.
`resume`, `settings`, `save_code`). aexp-owned (overwritten if you try to
pass them): `project`, `group`, `tags`, `config`, `notes`, `dir`, `mode`.
Use `prepare_tracker` if you need full control.

## 2. Bring your own `wandb.init` — `prepare_tracker`

Use this when your code already calls `wandb.init` — e.g. a per-item
inference loop that creates short-lived runs with a caller-specific `name`,
custom tables, and its own `finish` on completion. `prepare_tracker`
computes the disciplined payload without calling `wandb.init`; you splat it
into your own call and stamp the binding afterward.

```python
from aexp import prepare_tracker
import wandb

ctx = prepare_tracker(job, project="my-project", offline=True)
# ctx.init_kwargs is ready to splat; omits `name` / `job_type` so you own them.

run = wandb.init(
    **ctx.init_kwargs,                # project, group, tags, config, notes, dir, mode, reinit
    name=f"item-{item_id}-{seed}",    # caller-owned
    job_type="per-item-eval",         # caller-owned
    # resume=..., settings=...        # caller-owned
)
ctx.bind(run)                         # stamps job.doc["tracker"]

try:
    run.log({"loss": loss})
    run.log_artifact(wandb.Artifact("trace", type="eval"))
    run.summary["n_rounds"] = 12
finally:
    run.finish()
```

### Merge rule

If you pass kwargs to `wandb.init` that overlap with `ctx.init_kwargs`,
standard Python dict-splat rules apply — whichever appears later wins.
The example above splats `ctx.init_kwargs` *first* and adds caller kwargs
after, so caller kwargs win for any shared key. In practice, aexp only
emits keys a disciplined caller shouldn't be overriding (`project`,
`group`, `tags`, `config`, `notes`, `dir`, `mode`, `reinit`). If you need
to override `group` deliberately, it's your call; aexp won't stop you.

### Introspection

`TrackerContext` exposes `group`, `project`, `tags`, and `init_kwargs` as
public fields — read them if you want to, e.g., log the group string
somewhere else or verify the tags before the `wandb.init` call.

### `ctx.bind(run, *, backend="wandb")`

Duck-types `run.id` (required) and `run.url` (optional). Writes a
`TrackerBinding` into `job.doc["tracker"]` and returns it. Pass
`backend="mlflow"` or similar if you adapted the context to a non-wandb
tracker.

## 3. `TrackerAdapter` — CLI, subprocess / cluster, noop, custom backends

The `TrackerAdapter` ABC is the common shape behind `bind_tracker`. It
serves three distinct use cases:

- **CLI / subprocess / cluster workflows** where your training script runs
  somewhere aexp's Python API can't reach — e.g. a bash script that
  invokes a `torch.distributed` launcher, or a slurm job that runs a
  pre-existing training binary. You create the signac job via `aexp
  new-run`, bind wandb via `aexp bind-tracker <job_id> --backend wandb
  --project <name> [--offline]` from the login node, and the training
  script then just calls `wandb.init(...)` itself (or `aexp.prepare_tracker`
  if it *can* reach Python). The binding is already stamped.
- **Tests / local-only workflows** via `NoopAdapter` — writes JSONL events
  to the job workspace, no network, no wandb account needed.
- **Custom backends** — implement the ABC for MLflow, Aim, DVC, or
  anything else.

```python
class TrackerAdapter(ABC):
    name: str  # short backend name: "noop", "wandb", ...

    def init_run(self, *, project, group, tags, config, notes, offline, workspace) -> RunHandle: ...
    def log(self, handle, metrics) -> None: ...
    def log_artifact(self, handle, name, path) -> None: ...
    def finish(self, handle, *, exit_code=0) -> None: ...
    def list_runs(self, *, project, group_prefix) -> list[RunRecord]: ...
```

`bind_tracker(job, adapter, *, project, ...)` is the adapter-mediated entry
point. It internally calls the same derivation routine as `prepare_tracker`
(group slug, tags, config, notes) and passes the result to
`adapter.init_run(...)`, then stamps `job.doc["tracker"]`.

```python
from aexp import bind_tracker, NoopAdapter

handle = bind_tracker(job, NoopAdapter(), project="my-project")
# Noop writes JSONL to <workspace>/tracker_log/<run_id>/events.jsonl.
```

### `NoopAdapter` event format

```json
{"timestamp": "...", "event": "init_run",     "project": "...", "group": "...", "tags": [...], "config": {...}, "notes": "...", "offline": false}
{"timestamp": "...", "event": "log",          "metrics": {"loss": 0.1}}
{"timestamp": "...", "event": "log_artifact", "name": "out",    "path": "...", "size_bytes": 1234}
{"timestamp": "...", "event": "finish",       "exit_code": 0}
```

Default location: `<job_workspace>/tracker_log/<run_id>/events.jsonl`.
Pass `log_root=<path>` to `NoopAdapter(...)` for tests that aren't running
inside a real signac job.

### `WandbAdapter` via `bind_tracker`

```python
from aexp import bind_tracker, WandbAdapter

adapter = WandbAdapter(entity="my-team")  # entity optional
handle = bind_tracker(job, adapter, project="my-project", offline=True)
# The adapter owns wandb.init. Log via `adapter.log(handle, {...})` /
# `adapter.finish(handle)`, or reach through `handle.extra["run_object"]`
# for the raw wandb.Run and use wandb's full surface directly.
```

Equivalent in effect to `with tracked_run(job, project="my-project",
offline=True, entity="my-team") as run:` but you control the log /
finish lifecycle explicitly instead of via a context manager — useful
when the logging happens across function boundaries, in async code, or
in a subprocess you kicked off from here.

The CLI form is `aexp bind-tracker <job_id> --backend wandb --project
<name> [--offline] [--entity <team>]` — preferred when your training code
lives in a script you don't want to modify (see **Offline + sync
workflow** below for the cluster case).

## Offline + sync workflow (HPC)

Runs execute on compute nodes with no internet; you sync from a login node
afterward. Because `tracked_run` / `prepare_tracker` / the adapter all set
`dir=<job_workspace>`, offline runs land at predictable paths:

```
<repo>/.runs/workspace/<job_id>/wandb/offline-run-YYYYMMDD_HHMMSS-<id>/
```

### Compute-node side

Managed:

```python
with tracked_run(job, project="my-eval", offline=True) as run:
    ...
```

BYO-init:

```python
ctx = prepare_tracker(job, project="my-eval", offline=True)
run = wandb.init(**ctx.init_kwargs, name=f"item-{i}")
ctx.bind(run)
# ... work ...
run.finish()
```

CLI (adapter path):

```powershell
aexp new-run --experiment E018 --hypothesis H012 --sp condition=full,seed=0
aexp bind-tracker <job_id> --backend wandb --project my-eval --offline
```

### Login-node side

```powershell
# One command: walks .runs/workspace/*/wandb/, calls wandb sync on every offline run.
aexp sync-offline

# Preview without syncing:
aexp sync-offline --dry-run
```

Or drive wandb directly: `wandb sync --sync-all .runs/`.

Run IDs are stable between offline and online, so synced runs show up in
W&B with the same id, group (`H012/E018/full`), tags, and full run-link
config (`aexp.experiment_id`, `aexp.hypothesis_id`, etc.) regardless of
which mode initialized them.

### Python API

```python
from aexp import find_offline_runs, sync_offline_runs

paths = find_offline_runs(".runs")
results = sync_offline_runs(".runs", dry_run=False)
for r in results:
    if not r.ok:
        print(r.path, r.stderr)
```

## Writing a new adapter

1. Subclass `TrackerAdapter`, set `name`.
2. Lazy-import the backend SDK inside `__init__` or the methods — never at
   module load.
3. Preserve the contract: `init_run` returns a `RunHandle`, subsequent
   methods take it. Store any backend handle in `handle.extra`.
4. Register in `aexp/trackers/__init__.py` if you want it importable from
   the package root.
5. Add tests: mock the SDK (see `tests/test_trackers_wandb.py` for the
   pattern). Assert the init kwargs and that the adapter tolerates a
   missing backend (raises `TrackerInitError`).

If your backend supports a "bring your own run" pattern equivalent to
wandb's, mirror the `prepare_tracker` / `TrackerContext.bind` shape in your
own module. The adapter surface is one path among three; don't feel
obligated to route everything through it.

## Why no Weave / OpenTelemetry adapter in v1

Both were considered. Weave was rejected: the runtime is Claude Code /
Claude Desktop, which invokes the model inside a closed binary — our Python
never touches `anthropic.messages.create()`, so Weave's auto-instrumented
prompt/completion capture never fires. What's left is a generic function
tracer that doesn't justify the W&B-account + SDK weight.

OpenTelemetry is a plausible v1.1 extra (`pip install
agentic-experiments[otel]`): Claude Code itself emits OTEL under
`CLAUDE_CODE_ENABLE_TELEMETRY=1`, so our spans could land in the same
collector and correlate by session id. Not shipping in v1 — we don't yet
know whether structured JSON logs to stderr (which the aexp hooks already
produce) are enough.
