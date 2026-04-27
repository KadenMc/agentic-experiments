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
tag: "paper-ablation"              # optional; caller-supplied label for filtering
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
                       [--allow-dup-on-recommit]
aexp queue list        [--experiment E001] [--tag T] [--include-terminal]
aexp queue run         [--experiment E001] [--tag T] [--index N]
                       [--continue-on-failure] [--force] [--dry-run]
aexp queue remove      <job_id>
aexp queue stop        <job_id> [--grace-s 5] [--force]
aexp queue clear       [--experiment E001] [--tag T] [--yes]
aexp queue materialize [--runner shell|slurm|manual] [--output PATH] [--tag T]
                       [--slurm-time 04:00:00] [--slurm-mem 32G] [--slurm-gpus 1]
                       [--slurm-partition P] [--slurm-account A] [--slurm-extra "..."]
aexp run-queued        <job_id> [--force] [--dry-run]
```

### What `--tag` is (and isn't)

`tag` is **pure metadata** — a caller-supplied string aexp stores in
`job.doc["queue"]["tag"]` and never interprets. It has no semantics to
aexp: no scheduling, no deadlines, no wall-clock awareness. Every
queue-side verb accepts `--tag X` and filters to entries whose stored
tag equals `X`; that's the whole feature.

Why bother? Namespacing. When you have two batches queued concurrently
— a smoke test you submitted this morning and a paper ablation you're
queueing now — `materialize --tag paper-ablation` emits a runner for
just the paper batch, not both. Closest analog is a wandb group or a
git branch: a user-chosen label that lets bulk operations carve out a
specific slice.

Name tags by **what the batch is**, not when it runs. Good:
`paper-ablation`, `full-vs-classify`, `seed-stability`,
`reviewer-response`. Avoid temporal markers (`overnight`, `tonight`)
— they mislead readers into thinking aexp cares about timing. It
doesn't.

**`aexp queue run` is the primary cluster primitive.** It iterates the
pending queue (filtered by ``--experiment`` / ``--tag``) and executes
each matching job. Designed to be called *from inside* a batch script
you already wrote for your site — aexp owns the iteration and status
reconciliation; your script owns everything else (partition, account,
module loads, env activation, container setup).

Two shapes:

```bash
# Sequential — single-node, run every pending job in order:
aexp queue run --tag paper-ablation

# Array-parallel — one queued job per slurm array task:
#SBATCH --array=0-7
aexp queue run --tag paper-ablation --index "$SLURM_ARRAY_TASK_ID"
```

Jobs are enumerated in stable order (ascending ``queued_at``, then by
``job_id``) so ``--index N`` picks deterministically. Check count via
``aexp queue list --tag <tag>`` before sizing the ``#SBATCH --array``.

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
    clear_queue, materialize_queue, run_queued, run_queue, stop_queued,
    resolve_sp,
)
```

All surfaces — CLI, MCP tools (`queue_add`, `queue_list`, `queue_remove`,
`queue_stop`, `queue_clear`, `queue_materialize`), slash commands
(`/aexp-queue-add`, `/aexp-queue-list`, `/aexp-queue-materialize`,
`/aexp-queue-stop`) — are thin wrappers over the same Python API.

### Live observability + lifecycle control (0.2.1)

`aexp run-queued` streams subprocess output line-by-line to the parent's
stdout (`subprocess.Popen` with merged stderr, `bufsize=1`, immediate
flush per line). Interactive consumers — JupyterLab notebooks running
`aexp run-queued` directly — see live progress instead of waiting for
the buffered dump that 0.2.0's `subprocess.run(capture_output=True)`
forced. The last ~200 lines of merged output are kept in a ring buffer
and flushed into `job.doc["queue"]["last_error"].stderr_tail` (capped
at ~2 KB) on non-zero exit, so failure forensics are preserved.

`aexp queue stop <jobid>` interrupts a live run from another shell.
`run_queued` records the subprocess's pid, pgid, hostname, and a
process-start-time fingerprint in `job.doc["queue"]["proc"]` for the
duration of the run; `stop_queued` reads that, refuses to signal across
hosts, detects pid recycling on Linux via the fingerprint, and sends
SIGTERM to the process group followed by a SIGKILL escalation if the
runner ignores it. The job transitions to a new `"stopped"` terminal
status (distinct from `"failed"` / `"abandoned"`) with
`last_error.cause = "operator_stop"` so post-hoc forensics can tell
operator-stops from real failures.

`run_lifecycle` writes `doc["heartbeat_at"]` (ISO-8601 UTC) every
`heartbeat_s` seconds while a run is active (default 30 s; override
per-call, globally via `AEXP_HEARTBEAT_S`, or set to 0 to disable).
External liveness probes can compare `heartbeat_at` to wall-clock to
distinguish "still working" from "wedged" — a gap that 0.2.0's
write-once-at-start `status='running'` flag couldn't fill.

### Recommit deduplication (0.2.1)

`aexp queue add` (and sweeps) skip new entries whose sp matches an
existing pending entry's sp *modulo* the auto-injected `code_commit`
and `code_dirty` provenance fields. The common footgun this catches:
queue, fix a docstring, queue again — without dedupe, you get 2N
functionally identical pending jobs because the only sp diff is the
new commit hash.

The skip emits a `DuplicatePendingJobWarning`; the existing job is
returned. Pass `--allow-dup-on-recommit` (CLI) /
`allow_dup_on_recommit=True` (Python) when the recommit *is* the point
of the new entries — e.g. evaluating a fix in parallel with the
pre-fix queued runs.

Tag-scoped: different tags = different operational queues = no dedupe.
Terminal-status entries (complete / failed / abandoned / stopped) are
never deduped against — re-running a finished experiment is intentional.

### Shell-safe `{sp_json_shell}` placeholder (0.2.1)

`runner_command` templates that need the full resolved sp as a JSON
blob can use `{sp_json_shell}`, which applies `shlex.quote` to the
payload. Drop it in *unquoted*; shlex emits the shell quoting itself:

```yaml
# ✓ correct — shlex emits the quoting:
runner_command: "python -m mypkg.train {sp_json_shell}"

# ✗ wrong — double-quoting; nested-quote breakage:
runner_command: "python -m mypkg.train '{sp_json_shell}'"
```

The original `{sp_json}` is preserved unchanged for backward
compatibility, but its docstring now warns about the apostrophe trap
that breaks shell-quoted templates when an sp value contains the same
quote character. Use `{sp_json_shell}` for any shell-quoted context.

### Dirty-tree diff capture (0.2.1)

When `code_dirty=True`, `add_to_queue` writes a structured
`queue.code_diff_summary` blob alongside the bare `code_commit` SHA:

```python
job.doc["queue"]["code_diff_summary"] = {
    "diff_stat": "<git diff --stat HEAD>",
    "modified_count": <int>,
    "untracked_count": <int>,
}
```

Lets post-hoc forensics tell "queued from a clean commit" apart from
"queued with 12 modified files". Best-effort: capture is wrapped so a
queue add never fails because git is unavailable.

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
# 8 queued job(s) under tag=paper-ablation
set -e
cd "$(dirname "$0")"

aexp run-queued 9f3a1b2c...
aexp run-queued 7e2c4d1a...
# ... (one line per job)
```

Execute with `bash run.sh`. Sequential. Good for local runs or single-node
clusters.

### `--runner slurm` — a **starter template**, not a turn-key script

aexp has no visibility into your cluster's conventions (partition,
account, module loads, env activation, container setup, institutional
constraints), so the slurm runner emits a **starter template** with
explicit `# TODO` placeholders and commented-out setup lines for you to
fill in. The aexp-specific line at the bottom is the only thing that's
truly machine-generated:

```bash
#!/usr/bin/env bash
# Generated by `aexp queue materialize --runner slurm` at <ts>
# 8 queued job(s) under tag=paper-ablation
#
# ─────────────────────────────────────────────────────────────
# STARTER TEMPLATE — NOT A TURN-KEY SLURM SCRIPT.
# ...
# The only aexp-specific line is the final `aexp queue run`
# invocation. You can also skip this generated file entirely
# and call that line from your own existing batch script.
# ─────────────────────────────────────────────────────────────

#SBATCH --job-name=aexp-queue-paper-ablation
#SBATCH --array=0-7
#SBATCH --output=logs/aexp-%A-%a.out
#SBATCH --error=logs/aexp-%A-%a.err
# TODO: add #SBATCH --partition=... --account=... --time=...
# TODO: add #SBATCH --mem=... --gpus=... as your site requires.

# Site-specific setup — uncomment and edit as needed:
# module load cuda/12.1
# source ~/miniconda3/bin/activate <env>
# cd /path/to/this/repo

mkdir -p logs

# aexp resolves the pending-queue index → one specific job at run-time.
# If you re-queue jobs between submission and execution, the array will
# pick up whatever's pending under this filter at task-launch time.
exec aexp queue run --tag paper-ablation --index "$SLURM_ARRAY_TASK_ID"
```

**Why `aexp queue run --index` instead of baking job ids into a bash
array?** Job resolution is deferred to task-launch time, so re-queueing
between `materialize` and `sbatch` doesn't leave the array pointing at
stale ids. It also makes the generated file minimal — one line does the
aexp work; everything else is yours to tune.

Submit with `sbatch paper-ablation.sbatch`. Supply `#SBATCH` directives via
`--slurm-time`, `--slurm-mem`, `--slurm-gpus`, `--slurm-partition`,
`--slurm-account`, or the free-form `--slurm-extra` to pre-fill the
template when you invoke `materialize`.

**Or skip `materialize --runner slurm` entirely.** If you already have a
working slurm script for your site, just add one line:

```bash
#!/bin/bash
#SBATCH --array=0-7
# ... your existing #SBATCH block and site setup ...
aexp queue run --tag paper-ablation --index "$SLURM_ARRAY_TASK_ID"
```

That's the whole integration. `aexp queue run` is the intended surface
for any non-trivial cluster workflow; the generated template is a
convenience for first-time users.

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
aexp queue add --experiment E001 --sweep "condition=full|cls, seed=0..3" --tag paper-ablation
git add .runs/ kb/
git commit -m "queue paper ablation (H001/E001)"
git push

# Cluster (runner side) — using your own batch script:
git pull
# paper-ablation.sbatch is your site-tuned script, ending in:
#   aexp queue run --tag paper-ablation --index "$SLURM_ARRAY_TASK_ID"
sbatch paper-ablation.sbatch
# ... jobs execute, write status to .runs/workspace/<id>/signac_job_document.json ...
git add .runs/
git commit -m "run paper ablation"
git push

# Laptop (back on agent side):
git pull
aexp queue list --tag paper-ablation --include-terminal
# ... see 8 complete runs ready for /aexp-finding-from-batch ...
```

First-time cluster users can generate the starter template instead of
writing `paper-ablation.sbatch` from scratch:

```
aexp queue materialize --tag paper-ablation --runner slurm --output paper-ablation.sbatch
# Edit paper-ablation.sbatch to fill in #SBATCH directives + site setup.
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
