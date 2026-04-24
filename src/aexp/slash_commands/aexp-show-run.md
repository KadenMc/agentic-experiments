---
description: "Show state point, doc, and linked Limina frame for one signac run."
---

Show full detail for a single signac run — its state point, doc, status,
linked hypothesis/experiment, and tracker URL if bound. Read-only.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

Flow:

1. Ask the user for the job id (short prefix like `a3f2b1c4` or full
   32-hex is fine). If they don't know it, run `python -m aexp list-runs`
   first to show the available jobs, optionally filtered with
   `--experiment E###`.
2. Run:

   ```
   python -m aexp show-run <job_id>
   ```

3. Summarize the output for the user — pay attention to:
   - **Status** (`created` / `running` / `complete` / `failed` / `abandoned`)
   - **Experiment / hypothesis** linkage
   - **State point** — the identity-defining params
   - **Tracker URL** — click-through to wandb (or null if unbound / noop)
   - **Timestamps** — `started_at`, `ended_at`, `wallclock_s` if complete

4. If the user is reviewing a failing / broken run, suggest:
   - `python -m aexp validate --runs-only` to catch dangling citations
   - Checking `<workspace>/tracker_log/` (noop) or the wandb URL (wandb)
     for run-level error details.
