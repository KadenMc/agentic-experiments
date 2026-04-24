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

## Expected Outcome

- Confirm if: `{command}` -> {expected output or threshold}
- Reject if: `{command}` -> {expected output or threshold}

## Progress

- [ ] (`{date}`) {step}

## Results

_Link data files or summarize the result._

## Analysis

_What does the evidence actually say?_

## Decision

_What should happen next?_

## Links

{LINKS_BLOCK}
