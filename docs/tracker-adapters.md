# Tracker adapters

Tracker adapters translate a signac job + its linked Limina artifact into a
remote-backend run, without owning any canonical state. The ABC is
`TrackerAdapter`; two adapters ship in v1.

## `TrackerAdapter` contract

```python
class TrackerAdapter(ABC):
    name: str  # short backend name: "noop", "wandb", ...

    def init_run(self, *, project, group, tags, config, notes, offline) -> RunHandle: ...
    def log(self, handle, metrics) -> None: ...
    def log_artifact(self, handle, name, path) -> None: ...
    def finish(self, handle, *, exit_code=0) -> None: ...
    def list_runs(self, *, project, group_prefix) -> list[RunRecord]: ...
```

Return `RunHandle` from `init_run` with `id`, `url` (optional), `project`,
`group`, and backend-specific state in `extra={}`. The caller's
`bind_tracker` writes a `TrackerBinding` dict to `job.doc["tracker"]`:

```python
{
  "backend": "wandb",
  "run_id":  "abcdef12",
  "url":     "https://wandb.ai/...",
  "project": "my-project",
  "group":   "H012/E018/full",
}
```

That round-trips: you can find a signac job from a W&B run via
`config.job_id`, and find the W&B run from a signac job via
`job.doc["tracker"]["url"]`.

## `NoopAdapter` — always available

Writes a JSONL event log instead of hitting a network. Events:

```json
{"timestamp": "...", "event": "init_run",     "project": "...", "group": "...", "tags": [...], "config": {...}, "notes": "...", "offline": false}
{"timestamp": "...", "event": "log",          "metrics": {"loss": 0.1}}
{"timestamp": "...", "event": "log_artifact", "name": "out",    "path": "...", "size_bytes": 1234}
{"timestamp": "...", "event": "finish",       "exit_code": 0}
```

Default log location: `<job_workspace>/tracker_log/<run_id>/events.jsonl`.
Pass `log_root=<path>` to `NoopAdapter(...)` for tests that aren't operating
inside a real signac job (e.g. unit tests on the adapter itself).

## `WandbAdapter` — optional extra

Install: `pip install agentic-experiments[wandb]`.

```python
from aexp.trackers import WandbAdapter
adapter = WandbAdapter(entity="my-team")  # entity optional
```

Key behaviors:

- Lazy-imports `wandb` at construction; raises `TrackerInitError` if the
  package is missing. Nothing at package import time requires `wandb`.
- `init_run(..., offline=True)` forwards `mode="offline"` to `wandb.init`,
  for HPC nodes without internet. See **Offline + sync** below.
- `init_run` always passes `dir=<job_workspace>` to `wandb.init`, so every
  run's local wandb state (offline-run dirs, logs, caches) lives under the
  signac job. One directory per run — no sprawl into the consumer repo's CWD.
- `list_runs(project=..., group_prefix=...)` queries W&B's API for matching
  runs. Regex filter on `group`.

## Offline + sync workflow (HPC)

Runs execute on compute nodes with no internet; you sync from a login node
afterward. Because the adapter co-locates wandb state with the signac
workspace, offline runs land at predictable paths:

```
<repo>/.runs/workspace/<job_id>/wandb/offline-run-YYYYMMDD_HHMMSS-<id>/
```

### Compute-node side

```python
# Python
from aexp import create_run, bind_tracker, WandbAdapter, run_lifecycle

job = create_run(experiment_id="E018", hypothesis_id="H012",
                 statepoint={"condition": "full", "seed": 0})
bind_tracker(job, WandbAdapter(), project="ecg-inquiry-eval", offline=True)

with run_lifecycle(job):
    # ... work; all wandb writes go to job.path/wandb/ ...
```

Or via the CLI:

```powershell
aexp new-run --experiment E018 --hypothesis H012 --sp condition=full,seed=0
aexp bind-tracker <job_id> --backend wandb --project ecg-inquiry-eval --offline
```

### Login-node side

```powershell
# One command: walks .runs/workspace/*/wandb/, calls wandb sync on every offline run.
aexp sync-offline

# Preview without syncing:
aexp sync-offline --dry-run
```

Or, if you'd rather drive `wandb` directly:

```powershell
wandb sync --sync-all .runs/
```

Run IDs are stable between offline and online, so the synced runs show up
in W&B with the same id, group (`H012/E018/full`), tags, and full Limina
config (`limina.experiment_id`, `limina.hypothesis_id`, etc.).

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
4. Register in `aexp/trackers/__init__.py` if you want it
   importable from the package root.
5. Add tests: mock the SDK (see `tests/test_trackers_wandb.py` for the
   pattern). Assert the init kwargs and that the adapter tolerates a
   missing backend (raises `TrackerInitError`).

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
know whether structured JSON logs to stderr (which the Limina hooks already
produce) are enough.
