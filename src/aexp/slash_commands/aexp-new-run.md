---
description: "Create a new signac run linked to an experiment."
---

Create a tracked run for an existing experiment.

> **Invocation note.** The examples below use `python -m aexp`
> directly. If running from a Claude Code session where `python` does not
> resolve to the env that has `aexp` installed, prefix every
> command with the appropriate env selector:
>
> - **conda users** (most common):
>   `conda run -n <env-name> python -m aexp <verb>`
>   The env name was captured at install time — see
>   `.aexp/installed.json` field `conda_env_name`.
> - **venv users**:
>   `"<python_exe>" -m aexp <verb>`, where `<python_exe>`
>   is the absolute path from `.aexp/installed.json` field
>   `python_exe`.
>
> `aex <verb>` (the Poetry-installed shim) works in human terminals but may
> not be on Claude Code's Bash PATH, so avoid it from slash commands.

Ask the user for:
1. The experiment id (e.g. `E018`). If they don't know it, run
   `python -m aexp list-runs` first to show existing runs. If the
   experiment doesn't exist yet, write it to
   `kb/research/experiments/E###-<slug>.md` using `templates/experiment.md`
   as the starting point — the `PreToolUse` hook will validate the shape
   before the write lands.
2. The hypothesis id (optional — defaults to the experiment's primary
   hypothesis).
3. State-point params. At minimum `condition=<val>`; add `model=`, `seed=`,
   `dataset_slice=` if meaningful.

Then run:

```
python -m aexp new-run --experiment <E###> [--hypothesis <H###>] --sp condition=<val>[,key=val...]
```

After the job is created, show the user:
- The short job id.
- The workspace path (for writing outputs).
- Next step — how to attach a W&B run. Pick based on where the training
  code actually executes:
  - **Training code can import `aexp`** (notebook, inline script, or any
    training script that can `import aexp`). Preferred: use the Python API
    from inside the training code itself:
    ```python
    from aexp import open_run, tracked_run
    job = open_run("<job_id>")
    with tracked_run(job, project="<proj>", offline=True) as run:
        # run is a real wandb.Run; full wandb API available
        ...
    ```
    Or, if the training code already calls `wandb.init`, use
    `prepare_tracker(job)` + splat `ctx.init_kwargs` into `wandb.init`.
    See `docs/tracker-adapters.md` for the merge rules.
  - **Training runs in a script / binary that can't import `aexp`**
    (locked-down launcher, non-Python training). Stamp the binding from
    the CLI before the job runs:
    `python -m aexp bind-tracker <job_id> --backend wandb --project <proj> [--offline]`.
    This creates the aexp-disciplined wandb run (correct group / tags /
    config / notes) and writes the run id to `job.doc["tracker"]["run_id"]`.
    Your training script then resumes the same run with
    `wandb.init(resume="allow", id="<run_id>")` and logs normally.
    (You can read the run_id from `aexp show-run <job_id>`.)
  - **No W&B account / local-only**:
    `python -m aexp bind-tracker <job_id> --backend noop` — writes JSONL
    events to `<workspace>/tracker_log/`.
