---
description: "Draft a Finding (F###) citing a batch of runs by selector."
---

Close out a batch of runs — typically all runs with a given
`(experiment, condition)` — by drafting an `F###` Finding that cites the
batch selector.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

> **Backlink rule.** `kb_write_guard` enforces bidirectional wiki-links.
> The new finding, its hypothesis, and its experiment must all link each
> other in their `## Links` sections. Expect to edit three files, not one.

Flow:

1. Ask the user for `<experiment>` (E###) and the batch selector (usually
   `condition=<name>`, optionally with `model=<name>`). Run:

   ```
   python -m aexp list-batches --experiment <E###>
   python -m aexp show-batch --experiment <E###> --condition <cond>
   ```

   to confirm the batch has runs and what their status mix looks like.
2. Pick the next Finding id by scanning `kb/research/findings/F*.md` —
   use the smallest unused `F###`. Create the file at
   `kb/research/findings/F###-<slug>.md` from `templates/finding.md`.
   Fill `hypothesis:` and `experiment:` from the batch. The
   `PreToolUse` hook will reject a finding that doesn't name an existing
   experiment. Slug should describe the verdict
   (e.g. `paired-ablation-verdict`).
3. Open `kb/research/findings/F###-*.md` and add a **batch** citation in
   `supporting_runs:` — mapping form only, never a bare string:

   ```yaml
   supporting_runs:
     - type: batch
       experiment_id: "<E###>"
       selector:
         condition: "<condition>"
         # model: "<model>"   # if used
   ```

4. Pre-fill `## Results` with the batch aggregate (n, mean / min / max of
   each summary metric, proportion of runs with status=complete). Leave
   `## Decision` blank for the user.
5. **Add backlinks:** edit `H###-*.md` and `E###-*.md` to add `- [[F###]]`
   to their `## Links` sections. Don't skip this — validation requires it.
6. Run `python -m aexp validate` and confirm no
   `finding.empty_batch` or `finding.broken_run_citation` issues are
   reported.
