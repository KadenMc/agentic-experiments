---
description: "List pending queued runs, optionally filtered by experiment or tag."
---

Show every signac job currently marked `status="queued"` (or still
active — `running` — if the runner is in flight). Read-only.

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user what slice they want (or default to everything):
   - `--experiment <E###>` — one experiment's queued runs.
   - `--tag <tag>` — jobs grouped under that tag.
   - `--include-terminal` — also show historical queue entries in
     `complete` / `failed` / `abandoned`. Off by default because the
     queue view is about pending work.

2. Run:

   ```
   python -m aexp queue list [--experiment <E###>] [--tag <tag>] \
       [--include-terminal]
   ```

3. Summarize the table: count of queued, any in `running`, any in
   `failed` (if `--include-terminal`), plus the distinct sps. If nothing
   is pending for the user's filter, say so plainly.

4. If a `failed` job shows up in include-terminal mode, suggest
   `aexp show-run <short_id>` to see the last_error captured in
   `job.doc["queue"]["last_error"]` (stderr tail + returncode +
   failed_at).

5. If the user wants to run the pending batch: suggest
   `/aexp-queue-materialize` to emit a runner script. If they want to
   cancel: `python -m aexp queue remove <job_id>` (one) or
   `python -m aexp queue clear [--tag <tag>]` (bulk).
