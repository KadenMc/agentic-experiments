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
   - ``finding.broken_run_citation`` — the cited job id does not exist in
     `.aexp/ledger/`, the local `.runs/`, or any `.aexp/runs-index/`.
     Either update ``supporting_runs:``, re-run the job, or (if the
     run exists on another machine) run `aexp ledger backfill` there
     and `git pull` here.
   - ``finding.absent_run_citation`` (warning) — the cited job is in a
     peer machine's `.aexp/runs-index/<machine>.json` but not in this
     repo's ledger or local store. Run `aexp ledger backfill` on the
     peer machine + commit + push, then `git pull` here to upgrade
     the warning to a clean resolution.
   - ``finding.empty_batch`` — the batch selector matches zero runs; add
     runs or change the selector.
   - ``finding.absent_batch_runs`` (warning) — the batch selector matches
     only runs that live on other machines. Same fix as
     `absent_run_citation`: backfill the ledger on those machines.
   - ``finding.no_run_store`` (warning) — no source-of-truth for run
     identity is available (no ledger, no local store, no indexes).
     Run `aexp install` (creates the run store) and/or pull a ledger
     from a peer.
   - ``run.orphan`` — the signac job has no ``doc["aexp"]`` link;
     stamp one via `python -m aexp link <job_id> --experiment E###`.
   - ``run.broken_experiment_link`` — the experiment id in
     ``doc["aexp"]`` does not exist in ``kb/``; either create the
     experiment or re-link the run.
   - KB issues (``metadata``, ``links``, ``backlink``, ``reference``,
     ``aliases``) — fix the offending artifact; most common is a missing
     ``- [[X]]`` bullet in ``## Links``.

3. Offer to re-run validation after any fix.

**Cross-machine escape hatch.** If you're on a laptop and the cited
runs live on a cluster you can't see locally, pass `--strict-runs=warn`
to downgrade citation-existence failures to warnings (validator exits
0). The right long-term fix is for the cluster to `aexp ledger
backfill && git push`, then you `git pull` — that resolves the
citation cleanly with no warning needed.
