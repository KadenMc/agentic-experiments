---
id: "{ARTIFACT_ID}"
aliases: ["{ARTIFACT_ID}"]
type: hypothesis
status: PROPOSED
thread: "{THREAD_ID}"
created: "{DATE}"
last_updated: "{DATE}"
tags: []
---

# {ARTIFACT_ID} — {TITLE}

> **Status**: PROPOSED | TESTING | CONFIRMED | REJECTED
> **Created**: {DATE}
> **Last updated**: {DATE}

## Statement

_Clear, falsifiable claim._

## Mechanism

_What capability or failure mode do you think changes, and why?_

## Why This Might Generalize

_Why should this hold beyond the current eval slice or client-specific wording?_

## Shortcut Risks

_What could make this look good without improving the real capability?_

## Test Plan

_How will the hypothesis be tested? Pick the framing that fits and
delete the other block. Don't fabricate retroactive thresholds — write
honestly about what's pre-committed vs. what's exploratory._

_For pre-registered hypotheses_ (preferred for high-stakes / paper-cited
work):
- Experiment(s): ...
- Confirm if: `{command}` -> {observable threshold or result}
- Reject if: `{command}` -> {observable threshold or result}

_For exploratory hypotheses_ (smoke tests, early-project framing, no
committed thresholds yet):
- Experiment(s): ...
- Purpose: {what you want to see / learn from these runs}
- _No pre-registered confirm/reject criteria. If a future scaled
  replication needs them, this section gets revised at that point._

## Evidence

_Existing sources, prior artifacts, or measurements that motivated the hypothesis._

## Conclusion

_Fill this after testing._

## Links

{LINKS_BLOCK}
