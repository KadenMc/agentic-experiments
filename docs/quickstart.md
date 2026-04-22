# Quickstart

A worked example: fresh repo → hypothesis → experiment → two runs → batch finding.

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

- `kb/` seeded with Limina's template (`ACTIVE.md`, `DASHBOARD.md`, `mission/CHALLENGE.md`, `research/...`).
- `.claude/settings.json` with Python hooks enforcing the H→E→F chain.
- `.runs/` initialized as a signac project.
- `AGENTS.md` + `CLAUDE.md` merged in.

Open Claude Code in the repo. The `SessionStart` hook fires and injects your `kb/ACTIVE.md` + `kb/mission/CHALLENGE.md` so the agent knows the state of your research.

## 2. Frame a hypothesis

Creation goes through the vendored Limina script (since it handles ID allocation + templates):

```powershell
python scripts/kb_new_artifact.py hypothesis
```

Interactive prompts: slug, initial text. This creates `kb/research/hypotheses/H001-<slug>.md` from the Limina template. Fill in the `## Statement`, `## Mechanism`, `## Test Plan`, etc.

## 3. Frame an experiment

```powershell
python scripts/kb_new_artifact.py experiment
```

When asked, link to `H001`. Creates `kb/research/experiments/E001-<slug>.md`. If you want narrower framings within this experiment, add:

```yaml
sub_hypotheses: ["H002", "H003"]
```

to its frontmatter (create the H's first).

Fill in `## Objective`, `## Procedure`, `## Expected Outcome`. Consider writing a `## Local Hypothesis` section — it'll get pulled into tracker run notes.

## 4. Run the experiment

### From Python (recommended)

```python
from aexp import create_run, bind_tracker, NoopAdapter, run_lifecycle
import json

job = create_run(
    experiment_id="E001",
    hypothesis_id="H001",
    statepoint={"model": "gpt-oss-20b", "condition": "full", "seed": 0},
    # code_commit is auto-added; set include_commit=False for WIP iteration
)
# Optional mirror — noop writes JSONL into the job workspace; swap to WandbAdapter when ready.
handle = bind_tracker(job, NoopAdapter(), project="my-project")

with run_lifecycle(job):
    # ... your actual experiment code ...
    result = {"accuracy": 0.83, "n": 32}
    (job.fn("output.json")).write_text(json.dumps(result))
    # Log to the tracker (noop -> JSONL; wandb -> remote)
    from aexp.trackers import NoopAdapter as _noop  # use the adapter you bound
    # ...
```

`run_lifecycle` handles status transitions: `created` → `running` → `complete` (or `failed` if an exception propagates) + writes `started_at` / `ended_at` / `wallclock_s`.

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

## 6. Close out with a Finding

```powershell
/aexp-close-batch --experiment E001 --condition full
```

The slash command (installed by `aexp install-slash-commands`) walks the agent through drafting an `F###` that cites the batch:

```yaml
supporting_runs:
  - type: batch
    experiment_id: "E001"
    selector: { condition: "full" }
```

Fill in `## Verdict`, `## Analysis`, `## Decision`. Run `aexp validate` to confirm the finding's citation resolves.

## 7. Six months later

```powershell
aexp list-runs --experiment E001        # every run still there
aexp show-run <job_id>                  # full sp + doc + linked experiment
cat kb/research/findings/F001-*.md     # the conclusion you committed to
aexp validate                           # everything still hangs together
```

Reading path: `kb/ACTIVE.md` → `kb/research/findings/F*.md` → `Supporting runs:` → `aexp show-run <id>` → preserved workspace with outputs + tracker URL + original framing.
