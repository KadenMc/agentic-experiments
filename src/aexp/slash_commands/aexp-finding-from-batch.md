---
description: "Create a Limina finding (F###) citing a batch of runs by selector."
---

Create a new finding that cites a batch selector — typically all runs
with a given `(experiment, condition)` slice — as its supporting evidence.
Use this for ablations, sweeps, and any claim grounded in aggregate
behavior across N runs.

> **Three sibling commands create findings** — pick by what the finding
> cites:
>
> - **`/aexp-finding-from-batch`** (this command) — cites an
>   `(experiment, condition)` batch selector
> - **`/aexp-finding-from-run`** — cites one specific signac job
> - **`/aexp-finding-placeholder`** — no run citations yet (synthesis /
>   deferred)

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user for `<experiment>` (E###) and the batch selector — usually
   `condition=<name>`, optionally with `model=<name>`. Run:

   ```
   python -m aexp list-batches --experiment <E###>
   python -m aexp show-batch --experiment <E###> --condition <cond>
   ```

   to confirm the batch has runs and what their status mix looks like.
   If the batch has zero runs, stop — no point creating a finding with a
   selector that matches nothing.
2. Derive the finding title from the user — usually descriptive of the
   verdict (e.g. `paired-ablation-full-vs-classify-only`,
   `gateway-quality-parity-confirmed`). Ask for the impact level
   (`CRITICAL` | `HIGH` | `MEDIUM` | `LOW`; default `MEDIUM`).
3. Create the finding skeleton — `aexp new-finding` handles id allocation,
   template rendering, and patches both parents' `## Links` sections
   automatically:

   ```
   python -m aexp new-finding --title "<title>" \
       --hypothesis <H###> --experiment <E###> [--impact HIGH]
   ```

   Output reports the new `F###` id and path.
4. Open `kb/research/findings/F###-*.md` and add the batch citation to
   the YAML frontmatter as a **list of mappings** (mapping form only,
   never bare strings):

   ```yaml
   supporting_runs:
     - type: batch
       experiment_id: "<E###>"
       selector:
         condition: "<condition>"
         # model: "<model>"   # if used
   ```

5. Pre-fill the `## Evidence` section with the batch aggregate — `n`,
   mean / min / max of each relevant `summary_metric`, proportion of runs
   with `status=complete`. Fill `## Finding`, `## Caveats`,
   `## What Improved For Real`, `## Remaining Debt`, `## Next Move` from
   the user's analysis. Leave the `## Links` section alone —
   `new-finding` already set it up. Boundary reminder: `## Caveats`
   captures what limits interpretation of this finding (small `n`,
   batch composition, inherited experiment caveats); `## Remaining
   Debt` captures what's still a workaround in the system. Both
   required.
6. Run `python -m aexp validate` and confirm no `finding.empty_batch` or
   `finding.broken_run_citation` issues are reported.
