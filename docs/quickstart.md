# Quickstart

A worked example: fresh repo → hypothesis → experiment → two runs → batch finding.

> **Editing `aexp` itself?** If you're developing the `aexp` source *while* using it in this target repo, follow [Contributing → Developing `aexp` against one of your own research repos](../README.md#developing-aexp-against-one-of-your-own-research-repos) instead of this consumer-path install. The rest of this guide works identically once you're set up.

> **Invocation.** Examples below use `aexp <verb>` for brevity, but three
> forms exist. In decreasing order of robustness under Claude Code:
>
> - `conda run -n <env> python -m aexp <verb>` — most robust; works
>   everywhere `conda` is on PATH, no activation needed. Env name lives
>   in `.aexp/installed.json` → `conda_env_name`.
> - `python -m aexp <verb>` — works when the current `python` has the
>   package installed.
> - `aexp <verb>` — shortest; only on PATH from a human terminal with the
>   env active. Not reliable under Claude Code's Bash tool.
>
> All three run the same Typer app. Pick the one that fits your shell.

## 1. Install into your repo

```powershell
conda activate agentic-exp
cd path\to\my-research-project
aexp install
```

After 10 seconds:

- `kb/` seeded with the research-graph template (`ACTIVE.md`, `DASHBOARD.md`, `mission/CHALLENGE.md`, `research/...`).
- `.claude/settings.json` with Python hooks enforcing the H→E→F chain.
- `.runs/` initialized as a signac project.
- `AGENTS.md` + `CLAUDE.md` merged in.

Open Claude Code in the repo. The `SessionStart` hook fires and injects your `kb/ACTIVE.md` + `kb/mission/CHALLENGE.md` so the agent knows the state of your research.

> **Not ready to commit to a hypothesis yet?** If you're still in
> directional-exploration mode — iterating on a hunch that hasn't earned
> a tracked H yet — start with `/aexp-new-sandbox` (or
> `aexp new-sandbox --slug ...`). Sandbox subdirs live under
> `notebooks/_sandbox/<date>_<slug>/`, are explicitly outside the H→E→F
> enforcement chain, and are reversible (`git checkout` undoes
> everything). When the result matures into a directional prediction
> worth citing, walk it into the tracked chain via `/aexp-new-thread →
> /aexp-new-hypothesis → /aexp-promote-nb`. See
> [docs/sandbox.md](sandbox.md).

## 2. Frame a hypothesis

Artifact files live under `kb/research/hypotheses/<ID>-<slug>.md`. Templates sit in `templates/` — copy the hypothesis template, fill it in, and save with an allocated ID.

Inside Claude Code, the easiest path is to ask the agent: "Create hypothesis H001 for <topic>, using the template at templates/hypothesis.md." The `PreToolUse` hook enforces the expected path and frontmatter; the `PostToolUse` hook runs KB validation so you'll see any issues immediately.

From a shell, the same file-creation by hand works — YAML frontmatter plus the `## Statement`, `## Mechanism`, `## Test Plan` sections from the template.

> CLI verbs `aexp new-hypothesis`, `aexp new-experiment`, and `aexp new-finding` handle artifact creation in one command (plus automatic backlink patching on the parent files). See `aexp --help` or the slash-command set under `.claude/commands/aexp-new-*.md`.

## 3. Frame an experiment

Same pattern: `templates/experiment.md` → `kb/research/experiments/E001-<slug>.md`. The experiment **must** reference a live hypothesis — the `PreToolUse` hook blocks experiment creation without a `hypothesis: "H001"` frontmatter key (or `> **Hypothesis**: H001` blockquote).

If you want narrower framings within this experiment, add:

```yaml
sub_hypotheses: ["H002", "H003"]
```

to its frontmatter (create the H's first).

Fill in `## Objective`, `## Setup`, `## Procedure`, `## Caveats`, `## Intent` (pre-registered or exploratory — pick one), `## Progress`. The validator checks every shipped template header is present, so don't delete sections — fill placeholders. Consider writing a `## Local Hypothesis` section under `## Objective` — it'll get pulled into tracker run notes.

## 4. Run the experiment

### From Python (recommended)

Managed wandb run — aexp owns `wandb.init` + `run.finish`:

```python
from aexp import create_run, run_lifecycle, tracked_run

job = create_run(
    experiment_id="E001",
    hypothesis_id="H001",
    statepoint={"model": "gpt-oss-20b", "condition": "full", "seed": 0},
)

with run_lifecycle(job), tracked_run(job, project="my-project", offline=True) as run:
    # ... your actual experiment code ...
    run.log({"accuracy": 0.83, "n": 32})
    run.summary["final_accuracy"] = 0.83
```

Or, if your code already calls `wandb.init` and you just want aexp's
disciplined payload + signac binding:

```python
from aexp import create_run, prepare_tracker, run_lifecycle
import wandb

job = create_run(experiment_id="E001", hypothesis_id="H001",
                 statepoint={"condition": "full"})
ctx = prepare_tracker(job, project="my-project", offline=True)
run = wandb.init(**ctx.init_kwargs, name="my-run", job_type="eval")
ctx.bind(run)

with run_lifecycle(job):
    run.log({"accuracy": 0.83})
    run.finish()
```

Or, if you don't want wandb at all, use the always-available noop adapter:

```python
from aexp import bind_tracker, NoopAdapter
handle = bind_tracker(job, NoopAdapter(), project="my-project")
# Noop writes events to <job.workspace>/tracker_log/<run_id>/events.jsonl.
```

`run_lifecycle` handles signac status transitions: `created` → `running` → `complete` (or `failed` if an exception propagates) + writes `started_at` / `ended_at` / `wallclock_s`. It is orthogonal to tracker binding — compose both.

### From the CLI

```powershell
aexp new-run --experiment E001 --hypothesis H001 --sp condition=full,model=gpt-oss-20b
aexp bind-tracker <job_id> --backend noop
```

The CLI doesn't run your code for you — it creates the job + workspace. Your code opens the job (`open_run(<id>)`), writes outputs into `job.fn(...)`, and updates `job.doc`.

## 5. Run a few more, look at batches

```powershell
aexp new-run --experiment E001 --hypothesis H001 --sp condition=full,model=gpt-oss-20b,seed=1
aexp new-run --experiment E001 --hypothesis H001 --sp condition=classify,model=gpt-oss-20b,seed=0
aexp list-runs --experiment E001
aexp list-batches --experiment E001
aexp show-batch --experiment E001 --condition full
```

`list-batches` rolls up by `(experiment_id, condition)` by default — one row per distinct slice, with counts and status mix.

## 5b. Batch-queue for cluster / batched execution

If you want to queue N jobs and materialize them as a runner script — instead of calling `new-run` per job and executing inline — use the `queue` subcommand group:

```powershell
# Declare what the conditions mean (once, in E001's frontmatter):
#
#   runner_command: "python -m mypkg.train --config-json '{sp_json}'"
#   conditions:
#     full:     { model: "baseline", max_turns: 12 }
#     classify: { model: "baseline", max_turns:  4 }

# Queue 8 jobs in one call (Cartesian sweep):
aexp queue add --experiment E001 --sweep "condition=full|classify, seed=0..3" --tag paper-ablation

# Inspect pending work:
aexp queue list --tag paper-ablation

# Call `aexp queue run` from inside your own batch script — aexp iterates
# the pending queue, your script owns partition/account/modules/env:
cat > paper-ablation.sbatch <<'EOF'
#!/bin/bash
#SBATCH --array=0-7
#SBATCH --partition=your-partition
#SBATCH --time=04:00:00
# ... your site's other #SBATCH directives ...
source ~/miniconda3/bin/activate your-env
cd /path/to/repo
aexp queue run --tag paper-ablation --index "$SLURM_ARRAY_TASK_ID"
EOF

# Commit, push, pull on cluster, `sbatch paper-ablation.sbatch`. Or, if you
# prefer a starter template, `aexp queue materialize --runner slurm` emits
# one with clearly-marked TODO placeholders. See docs/queue.md for the
# full cross-machine sync workflow.
```

Each queued job freezes its full resolved config (the `conditions.full` block merged with whatever you passed in `--sp`/`--sweep`) to `signac_statepoint.json` — editing `conditions.full` tomorrow doesn't retroactively change what last night's runs did. That's the drift-proof provenance mechanism.

Full story in [docs/queue.md](queue.md) (sp resolution rules, `{sp_json}` placeholder, runner script emitters, failure handling).

## 6. Write a Finding

```powershell
/aexp-finding-from-batch --experiment E001 --condition full
```

Three sibling slash commands create findings — pick by what the finding cites: `/aexp-finding-from-run` (one job), `/aexp-finding-from-batch` (a batch selector), or `/aexp-finding-placeholder` (no citations yet). The slash command walks the agent through calling `aexp new-finding` (which handles id allocation + automatic parent-backlink patching) and then filling in the `supporting_runs:` citation:

```yaml
supporting_runs:
  - type: batch
    experiment_id: "E001"
    selector: { condition: "full" }
```

Fill in `## Finding`, `## Evidence`, `## Caveats`, `## What Improved For Real`, `## Remaining Debt`, `## Next Move`. The slash command's flow walks the agent through each section. Run `aexp validate` to confirm the finding's citation resolves and every shipped template header is present.

## 7. Six months later

```powershell
aexp list-runs --experiment E001        # every run still there
aexp show-run <job_id>                  # full sp + doc + linked experiment
cat kb/research/findings/F001-*.md     # the conclusion you committed to
aexp validate                           # everything still hangs together
```

Reading path: `kb/ACTIVE.md` → `kb/research/findings/F*.md` → `Supporting runs:` → `aexp show-run <id>` → preserved workspace with outputs + tracker URL + original framing.
