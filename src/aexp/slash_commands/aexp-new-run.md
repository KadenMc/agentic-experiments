---
description: "Create a new signac run linked to a Limina experiment."
---

Create a tracked run for an existing Limina experiment.

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
   `python -m aexp list-runs` first to show existing runs,
   or suggest they create the experiment via
   `python scripts/kb_new_artifact.py experiment`.
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
- Next step: if they want a W&B mirror, run
  `python -m aexp bind-tracker <job_id> --backend wandb --project <proj>`;
  if not, they can skip or use the noop tracker via
  `python -m aexp bind-tracker <job_id> --backend noop`.
