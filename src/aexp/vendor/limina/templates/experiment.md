---
id: "{ARTIFACT_ID}"
aliases: ["{ARTIFACT_ID}"]
type: experiment
status: DESIGNED
hypothesis: "{HYPOTHESIS_ID}"
created: "{DATE}"
completed: ""
tags: []
# Optional: command template invoked by `aexp run-queued`. Placeholders:
#   {key}     — substituted against the resolved state point. Any sp key.
#   {sp_json} — full resolved sp serialized as JSON (compact separators).
#   {job_id}  — full 32-hex signac job id.
# Example:
#   runner_command: "python -m mypkg.train --config-json '{sp_json}'"
runner_command: ""
# Optional: named condition blocks merged into a job's sp when
# `--sp condition=<name>` matches a key here. See docs/queue.md. Example:
#   conditions:
#     full:
#       model: "baseline"
#       max_turns: 12
#       tools: ["investigate", "classify", "retrieve"]
#     classify_only:
#       model: "baseline"
#       max_turns: 4
conditions: {}
---

# {ARTIFACT_ID} — {TITLE}

> **Status**: DESIGNED | RUNNING | COMPLETED | FAILED
> **Hypothesis**: [[{HYPOTHESIS_ID}]]
> **Created**: {DATE}
> **Completed**: {DATE}

## Objective

_What are you trying to learn or measure?_

## Setup

- Environment:
- Data:
- Compute:
- Dependencies:

## Procedure

1. ...
2. ...
3. ...

## Caveats

_Known limitations, deviations from plan, instrumentation gaps, or
other context that qualifies how the results should be read. Be
explicit about what wasn't measured. ``_None._`` is a valid answer
for fully-instrumented runs, but most experiments accumulate
something worth noting here._

## Intent

_What you want to learn from this experiment. Pick the framing that
fits and delete the other block. Don't fabricate retroactive
thresholds — write honestly about what's pre-committed vs. what's
exploratory._

_For pre-registered experiments_ (preferred for high-stakes /
paper-cited work):
- Confirm if: `{command}` -> {expected output or threshold}
- Reject if: `{command}` -> {expected output or threshold}

_For exploratory / smoke-test runs_ (no committed thresholds):
- Purpose: {one-line of what you want to see}
- _No pre-registered confirm/reject criteria. See [[{HYPOTHESIS_ID}]]'s
  Test Plan for where formal criteria would live in a scaled
  replication._

## Progress

- [ ] (`{date}`) {step}

## Results

_Link data files or summarize the result. Numbers, paths, run ids —
the raw record of what happened._

## Outcome Summary

_Experiment-level observations: what we saw in this specific run.
Generalizable claims belong in the linked Finding(s), not here. If
no finding has been authored yet, this section can carry "what we
learned" until one does._

## Decision

_What should happen next at the experiment level — re-run with new
seeds, abandon, spin off a follow-up E? Claim-level next-actions
(e.g. "ship this to the paper") live in the Finding's ``## Next
Move``._

## Links

{LINKS_BLOCK}
