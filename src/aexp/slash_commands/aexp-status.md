---
description: "One-shot read-only dashboard: artifact counts, recent runs, validation."
---

Print a quick snapshot of the harness state: how many H/E/F artifacts
exist, how many signac runs, whether validation is clean. Read-only —
makes no writes.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Count artifacts by kind:

   ```
   ls kb/research/hypotheses/H*.md 2>/dev/null | wc -l
   ls kb/research/experiments/E*.md 2>/dev/null | wc -l
   ls kb/research/findings/F*.md 2>/dev/null | wc -l
   ```

2. Show the current batches (one row per distinct (experiment, condition)):

   ```
   python -m aexp list-batches
   ```

3. Run validation and report the verdict:

   ```
   python -m aexp validate
   ```

4. Present a single summary to the user, e.g.:

   > H: 3, E: 7, F: 2 | runs: 64 across 9 batches | validate: OK

   If validation fails, surface the top 3–5 issue codes verbatim.
