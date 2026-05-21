---
description: "Run full aexp validation and surface every issue by code + path."
---

Run `python -m aexp validate` and report. The PostToolUse + Stop hooks
only cover structural KB checks; this also catches broken finding
citations and orphan signac jobs.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Run:

   ```
   python -m aexp validate
   ```

2. If it passes, confirm OK and stop. Otherwise list each issue as
   ``<code> [<path>]: <message>`` and, for each one, suggest the likely
   fix:
   - ``finding.broken_run_citation`` — the cited job id no longer exists;
     update ``supporting_runs:`` or re-run the job.
   - ``finding.empty_batch`` — the batch selector matches zero runs; add
     runs or change the selector.
   - ``run.orphan`` — the signac job has no ``doc["aexp"]`` link;
     stamp one via `python -m aexp link <job_id> --experiment E###`.
   - ``run.broken_experiment_link`` — the experiment id in
     ``doc["aexp"]`` does not exist in ``kb/``; either create the
     experiment or re-link the run.
   - KB issues (``metadata``, ``links``, ``backlink``, ``reference``,
     ``aliases``) — fix the offending artifact; most common is a missing
     ``- [[X]]`` bullet in ``## Links``.

3. Offer to re-run validation after any fix.
