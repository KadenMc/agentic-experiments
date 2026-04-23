---
description: "List signac runs with optional experiment / hypothesis / status filters."
---

Print a table of signac runs, optionally filtered.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask what slice the user wants (they may say "everything"). Any combo:
   - ``--experiment E###`` — one experiment's runs
   - ``--hypothesis H###`` — all runs linked to a hypothesis
   - ``--status <created|running|complete|failed|abandoned>``
   - ``--sp key=val,key2=val2`` — exact-match state-point filter

2. Run:

   ```
   python -m aexp list-runs [filters]
   ```

3. If the user wants aggregate counts by batch, follow up with
   `python -m aexp list-batches [--experiment E###]`.
