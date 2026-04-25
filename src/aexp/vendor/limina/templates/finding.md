---
id: "{ARTIFACT_ID}"
aliases: ["{ARTIFACT_ID}"]
type: finding
hypothesis: "{HYPOTHESIS_ID}"
experiment: "{EXPERIMENT_ID}"
impact: "{IMPACT}"
created: "{DATE}"
tags: []
---

# {ARTIFACT_ID} — {TITLE}

> **Created**: {DATE}
> **Hypothesis**: [[{HYPOTHESIS_ID}]]
> **Experiment**: [[{EXPERIMENT_ID}]]
> **Impact**: {IMPACT} | CRITICAL | HIGH | MEDIUM | LOW

## Finding

_One clear statement of what the experiment established._

## Evidence

_What measurements, comparisons, or observations support it?_

## Caveats

_What qualifies how this finding should be interpreted? Small sample
size, domain shift between eval and production, instrumentation
caveats inherited from the supporting experiment(s), confounds
identified after the fact. Distinct from ``## Remaining Debt``:
caveats are about **what limits this finding's interpretation**;
remaining debt is about **what's still a workaround in the system**._

## What Improved For Real

_What capability changed beyond the benchmark number itself?_

## Remaining Debt

_What is still a workaround, shortcut, or open risk?_

## Next Move

_What should the research do because of this finding?_

## Links

{LINKS_BLOCK}
