---
description: "Create a Limina finding (F###) citing one specific signac run."
---

Create a new finding that cites a single signac run as its supporting
evidence. Use this when the finding's claim is grounded in exactly one
job — a single eval, a single training run, a one-off experiment.

> **Three sibling commands create findings** — pick by what the finding
> cites:
>
> - **`/aexp-finding-from-run`** (this command) — cites one specific signac job
> - **`/aexp-finding-from-batch`** — cites an `(experiment, condition)`
>   batch selector
> - **`/aexp-finding-placeholder`** — no run citations yet (synthesis /
>   deferred)

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

Flow:

1. Ask the user for the `<job_id>` (short or full hex). Run
   `python -m aexp show-run <job_id>` to confirm the run exists, see its
   linked experiment/hypothesis, and grab any `summary_metrics` the run
   recorded.
2. Derive the finding title from the user — typically
   `<experiment>-verdict` or a short claim like
   `gateway-quality-parity-confirmed`. Ask for the impact level
   (`CRITICAL` | `HIGH` | `MEDIUM` | `LOW`; default `MEDIUM`).
3. Create the finding skeleton — `aexp new-finding` handles id allocation,
   template rendering, and patches both parents' `## Links` sections
   automatically:

   ```
   python -m aexp new-finding --title "<title>" \
       --hypothesis <H###> --experiment <E###> [--impact HIGH]
   ```

   Output reports the new `F###` id and path.
4. Open `kb/research/findings/F###-*.md` and add the run citation to the
   YAML frontmatter as a **list of mappings** (never bare strings):

   ```yaml
   supporting_runs:
     - type: job
       id: "<full-32-hex-job-id>"
   ```

5. Pre-fill the `## Evidence` section with anything useful from
   `job.doc["summary_metrics"]` (if present), the tracker URL (from
   `job.doc["tracker"]["url"]`), and any artifacts the run recorded.
   Fill in `## Finding`, `## What Improved For Real`, `## Remaining Debt`,
   `## Next Move` from the user's analysis. Leave the ``## Links`` section
   alone — `new-finding` already set it up.
6. Run `python -m aexp validate` and confirm no
   `finding.broken_run_citation` issues are reported.
