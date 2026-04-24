---
description: "Register pending runs — one or many — under an experiment."
---

Queue one or many pending runs against an existing experiment. Runs
don't execute immediately — they get `status="queued"` and wait for
`/aexp-queue-materialize` to emit a runner script.

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

Flow:

1. Ask the user for:
   - **Experiment** (`E###`). Must exist on disk.
   - **State-point values** — either one fixed combination (`--sp key=val,key=val`),
     or a sweep like `condition=full|classify_only, seed=0..3` that expands
     to a Cartesian product.
   - **Tag** (optional) — a short string that groups the queued jobs so
     `queue list`, `materialize`, and `clear` can filter. Example:
     `overnight-ablation`.

2. **Condition resolution.** If the `sp` includes a `condition` key and the
   experiment's frontmatter declares a `conditions:` block with a matching
   name, aexp merges the block into the sp *at queue-time* — the resolved
   config is frozen to signac's `signac_statepoint.json`. This is the
   drift-proof provenance mechanism documented in `docs/queue.md`. Don't
   do anything special; just tell the user if the experiment has
   conditions declared (they're inspectable via
   `aexp show-batch --experiment <E###>`).

3. Run one of:

   ```
   # Single job:
   python -m aexp queue add --experiment <E###> \
       --sp "condition=<name>,seed=<n>" \
       [--tag <tag>] [--hypothesis <H###>]

   # Bulk via sweep (Cartesian product over the sweep keys):
   python -m aexp queue add --experiment <E###> \
       --sweep "condition=full|classify_only, seed=0..3" \
       [--sp key=val,...]  \
       [--tag <tag>]
   ```

   Grammar reminders for `--sweep`:
   - `|` separates enumerated values: `condition=full|classify_only`.
   - `a..b` is an inclusive integer range: `seed=0..3`.
   - Multiple keys comma-separated; the product is taken across all of them.
   - Values in `--sp` are fixed; the same key cannot appear in both.

4. Output reports the queued job count and each job's short id. If the
   user wants to see the full resolved sp (with the merged condition
   block), follow up with `/aexp-queue-list --tag <tag>` or
   `aexp show-run <short_id>`.

5. Next step: suggest `/aexp-queue-materialize --tag <tag>` when the
   user is ready to produce a runner script.
