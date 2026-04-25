---
description: "Show all runs matching an (experiment, condition) batch selector."
---

Show every signac run matching a batch selector — typically one
`(experiment_id, condition)` slice. Read-only. Useful before drafting a
finding, to confirm the batch has the expected run count + status mix.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user for the `<experiment_id>` (e.g. `E018`) and the batch
   selector — usually just `--condition <name>`, optionally also
   `--model <name>`. If they're not sure which batches exist for an
   experiment, run `python -m aexp list-batches --experiment <E###>`
   first to show the distinct slices.
2. Run:

   ```
   python -m aexp show-batch --experiment <E###> --condition <cond> [--model <model>]
   ```

3. Summarize for the user — highlight:
   - **Count** of runs in the batch
   - **Status mix** (how many complete / failed / running / created)
   - **Tracker group** — the deterministic `H###/E###/condition` slug
   - Any runs that look anomalous (different sp than peers, no
     tracker_url when others have one, etc.)

4. If the user is preparing to close out a finding, follow up with
   `/aexp-finding-from-batch` — it wants the same selector you just
   inspected.
