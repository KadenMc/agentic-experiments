# Queue + runner materialization + sp resolution

`aexp queue` lets an agent register N pending experiment runs on one
machine and materialize them as a runner script that can execute on a
different machine. The design target is the case where the agent (MCP
host) can't reach the runtime environment — e.g. Claude Desktop on a
laptop and training on an HPC cluster accessed only via SSH. The queue
is a declarative artifact; *what runs where* is entirely up to the user's
chosen runner.

## The three problems this solves

1. **Intent vs. execution.** Queueing N jobs is one mental action; running
   them is another. The queue lets the user (or the agent) say *"I intend
   to run these 8 configurations tonight"* without spinning anything up.
2. **Cross-machine workflow.** The agent creates `.runs/` state; the user
   materializes a script; the runner (on a different machine) executes it;
   signac's on-disk state reports back via git.
3. **Provenance of condition labels.** Bare labels like `condition=full`
   leak provenance when training code changes — what "full" meant last
   week may not be what it means now. The `conditions:` frontmatter block
   on an experiment is the source of truth, and aexp resolves names
   against it *at queue-time* so the resolved config is frozen to each
   signac job.

## Data model

### Job status

`RunStatus` is extended with `"queued"`:

```
created → queued → running → complete
                           → failed
                           → abandoned (remove_from_queue)
```

- `create_run(...)` → `"created"` (unchanged).
- `add_to_queue(...)` → creates via `create_run`, then sets `"queued"`.
- `aexp run-queued <id>` → `"running"` on start, `"complete"`/`"failed"`
  on exit.
- `aexp queue remove <id>` → `"abandoned"`.

### `job.doc["queue"]`

Present on queued (or once-queued) jobs. Shape:

```yaml
queued_at: "2026-04-23T18:02:14Z"
tag: "overnight-ablation"          # optional; groups queued jobs
runner_hint: "slurm"                # optional; default for materialize
runner_command_override: "..."      # optional; supersedes experiment template
last_error:                         # set by run-queued on failure
  returncode: 1
  stderr_tail: "..."                # last ~2KB
  failed_at: "..."
```

### Experiment frontmatter: `runner_command` + `conditions`

The linked `E###` markdown's frontmatter declares how to run jobs and
what named conditions mean:

```yaml
---
id: "E001"
type: experiment
hypothesis: "H001"
runner_command: "python -m mypkg.train --config-json '{sp_json}'"
conditions:
  full:
    model: "baseline"
    max_turns: 12
    tools: ["investigate", "classify", "retrieve"]
    temperature: 0.2
  classify_only:
    model: "baseline"
    max_turns: 4
    tools: ["classify"]
    temperature: 0.2
---
```

`runner_command` is optional; jobs whose experiment has no template must
set `runner_command_override` per-job, or `aexp run-queued` raises
`RunnerCommandMissing`.

`conditions` is optional; experiments without it preserve the bare-label
behavior (`--sp condition=full` just stores the string `"full"`).

Both fields are version-controlled in git like the rest of `kb/`, so
*"what did `full` mean on 2026-04-23?"* is answerable by `git log -p
kb/research/experiments/E001-*.md`.

## Runner-command placeholders

`render_runner_command` substitutes against the job's resolved sp plus
two synthetic keys:

| Placeholder | Value |
|---|---|
| `{key}` | `str(sp[key])` — any field in the job's state point |
| `{sp_json}` | Full resolved sp serialized as JSON (compact separators; no whitespace — critical for shell transport) |
| `{job_id}` | Full 32-hex signac job id |

Unknown `{xxx}` placeholders are left as-is so shell variables pass
through untouched (regex matches `{…}` only, not `${…}`).

Two usage patterns:

**Thread specific keys** (simple sps):
```yaml
runner_command: "python train.py --condition {condition} --seed {seed}"
```

**Pass full config as JSON** (recommended for non-trivial configs):
```yaml
runner_command: "python -m mypkg.train --config-json '{sp_json}'"
```
```python
# Training script:
import argparse, json
p = argparse.ArgumentParser()
p.add_argument("--config-json")
cfg = json.loads(p.parse_args().config_json)
# cfg has every resolved sp key: model, max_turns, tools, seed, etc.
```

**Or read from the signac workspace** (no argv at all):
```python
import json, os
from pathlib import Path
sp = json.loads(
    (Path(os.environ["AEXP_JOB_WORKSPACE"]) / "signac_statepoint.json").read_text()
)
```
`AEXP_JOB_ID` and `AEXP_JOB_WORKSPACE` are injected into the subprocess
environment by `aexp run-queued`.

## sp resolution (drift-proof provenance)

When `aexp queue add --experiment E001 --sp condition=full,seed=0` runs:

1. aexp loads `E001`'s frontmatter.
2. Finds `conditions.full`.
3. Merges the block into sp: `sp = {**conditions_full, **user_sp}`.
   User-supplied keys win on collision.
4. Passes the merged sp to `create_run` — signac hashes on the full sp
   and writes it to `<workspace>/signac_statepoint.json`. The config is
   **frozen**: a later edit to `conditions.full` cannot change it.

Same behavior for `aexp new-run`: resolution is on by default via the
`resolve_conditions=True` kwarg on `create_run`. Turn it off with
`aexp queue add --no-resolve` (or `resolve_conditions=False` in Python)
if you deliberately want to store a bare label.

## Command surface

### CLI

```
aexp queue add         --experiment E001 [--sp K=V,...] [--sweep "K=V|V,K=a..b"] [--tag T] [--hypothesis H001]
aexp queue list        [--experiment E001] [--tag T] [--include-terminal]
aexp queue remove      <job_id>
aexp queue clear       [--experiment E001] [--tag T] [--yes]
aexp queue materialize [--runner shell|slurm|manual] [--output PATH] [--tag T]
                       [--slurm-time 04:00:00] [--slurm-mem 32G] [--slurm-gpus 1]
                       [--slurm-partition P] [--slurm-account A] [--slurm-extra "..."]
aexp run-queued        <job_id> [--force] [--dry-run]
```

### Sweep grammar

`--sweep "KEY1=V1|V2|V3, KEY2=a..b"`:

- `|` separates enumerated values. Values that parse as integers become
  ints; others stay strings: `seed=0|1|2` → `[0, 1, 2]`;
  `condition=full|cls` → `["full", "cls"]`.
- `a..b` (integers only) is an inclusive range: `seed=0..3` → `[0,1,2,3]`.
- Multiple keys comma-separated; Cartesian product across all keys.
- Keys in `--sp` are fixed for every job; the same key cannot appear in
  both `--sp` and `--sweep` (ambiguous).

### Python API

```python
from aexp import (
    add_to_queue, add_many_to_queue, list_queue, remove_from_queue,
    clear_queue, materialize_queue, run_queued, resolve_sp,
)
```

All surfaces — CLI, MCP tools (`queue_add`, `queue_list`, `queue_remove`,
`queue_clear`, `queue_materialize`), slash commands (`/aexp-queue-add`,
`/aexp-queue-list`, `/aexp-queue-materialize`) — are thin wrappers over
the same Python API.

### MCP caveat: no `run_queued` tool

The queue MCP surface deliberately does **not** expose `run_queued` as a
tool. Execution on the MCP host is nearly always the wrong env (agent's
laptop vs. user's cluster), and the failure modes of running there are
noisier than the convenience of enabling it. The agent's job is to queue
and materialize; the user (or whatever automation they wire up) invokes
the script wherever execution actually belongs.

## Materialized runner scripts

### `--runner shell`

```bash
#!/usr/bin/env bash
# Generated by `aexp queue materialize` at <ts>
# 8 queued job(s) under tag=overnight
set -e
cd "$(dirname "$0")"

aexp run-queued 9f3a1b2c...
aexp run-queued 7e2c4d1a...
# ... (one line per job)
```

Execute with `bash run.sh`. Sequential. Good for local runs or single-node
clusters.

### `--runner slurm`

```bash
#!/usr/bin/env bash
# Generated by `aexp queue materialize` at <ts>
# 8 queued job(s) under tag=overnight
#SBATCH --job-name=aexp-queue-overnight
#SBATCH --array=0-7
#SBATCH --output=logs/aexp-%A-%a.out
#SBATCH --error=logs/aexp-%A-%a.err
#SBATCH --time=04:00:00
#SBATCH --mem=32G
#SBATCH --gpus=1

jobs=(
  9f3a1b2c...
  7e2c4d1a...
  # ...
)

mkdir -p logs
exec aexp run-queued "${jobs[$SLURM_ARRAY_TASK_ID]}"
```

Submit with `sbatch overnight.sbatch`. Each array task runs one queued
job. Supply `#SBATCH` directives via `--slurm-time`, `--slurm-mem`,
`--slurm-gpus`, `--slurm-partition`, `--slurm-account`, or the free-form
`--slurm-extra` (passed through verbatim).

### `--runner manual`

Plain list of `aexp run-queued <id>` lines with a header comment. No
shebang, no control flow. Useful when your runner is qsub, LSF, Airflow,
or anything else that wants the commands but not the structure.

## Idempotency

Materialized scripts are safe to re-run:

- `aexp run-queued <id>` skips jobs whose status is already
  `complete`/`failed`/`abandoned`, prints `"skipping <id>: already <status>"`,
  and exits 0.
- To re-run a failed job after fixing the training code:
  `aexp run-queued <id> --force`.
- To re-queue a job entirely (new signac hash if sp changed, same
  workspace if sp unchanged): `aexp queue add` again with the same sp.

## Cross-machine sync workflow

aexp doesn't ship rsync helpers or shared-filesystem assumptions. The
recommended flow is git-sync of `.runs/`:

```
# Laptop (agent side):
aexp queue add --experiment E001 --sweep "condition=full|cls, seed=0..3" --tag overnight
aexp queue materialize --tag overnight --runner slurm --output overnight.sbatch
git add .runs/ overnight.sbatch kb/
git commit -m "queue overnight ablation (H001/E001)"
git push

# Cluster (runner side):
git pull
sbatch overnight.sbatch
# ... jobs execute, write status to .runs/workspace/<id>/signac_job_document.json ...
git add .runs/
git commit -m "run overnight ablation"
git push

# Laptop (back on agent side):
git pull
aexp queue list --tag overnight --include-terminal
# ... see 8 complete runs ready for /aexp-finding-from-batch ...
```

### What goes in git

`signac_job_document.json` (the `job.doc` JSON) is small and diff-friendly
— include it. The workspace's bulk outputs (model checkpoints, large
trace files, logs) are usually gitignored per repo convention. The
queue only needs the signac doc to reconcile status; everything else
is trace data.

### Merge conflicts

If both sides update the same job's doc concurrently (rare), you'll get
a merge conflict on `signac_job_document.json`. Resolve manually. The
recommended pattern is:

- Laptop writes **before** execution (add/materialize).
- Cluster writes **during/after** execution (run-queued → status transitions).
- Laptop doesn't touch `.runs/` while the cluster is running.

## FAQ

**What happens if the runner script is killed mid-job?**
Jobs that were `running` when the script died stay at `running` — the
status isn't automatically reconciled (no liveness tracking). Re-running
the script will pick them up because `run-queued` only skips terminal
states. If you need to wipe a stuck `running` state, either
`aexp queue remove <id>` (marks abandoned) or set `status="queued"`
manually.

**Why does `run-queued` write the failed job's stderr tail into
`job.doc`?**
So `aexp queue list --include-terminal` (or a `show-run`) can surface
why a job failed without hunting through slurm logs. Limited to last
2KB of stderr per job to keep docs small.

**Can I queue jobs against an experiment that has no `conditions:`
block?**
Yes. `--sp condition=full` stores `"full"` as a bare label (unchanged
behavior). The `conditions:` block is strictly opt-in; experiments
without it pay nothing and behave exactly as before.

**What if I want to override `max_turns` for just one queued job?**
Put it in `--sp`: `aexp queue add --experiment E001 --sp condition=full,max_turns=16`.
User sp keys win over condition-block values on collision, so that one
job gets `max_turns=16` while `conditions.full`'s other keys still merge in.

**Can I re-run a completed job without changing anything?**
`aexp run-queued <id> --force`. The signac workspace is reused (same
sp → same hash). Outputs will be overwritten.
