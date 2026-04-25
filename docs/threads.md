# Threads — `T###` artifacts

A **thread** is a forward-looking research concern, question, or
direction that's broader than a single hypothesis. Threads exist
because the H/E/F grammar — falsifiable claim → designed experiment →
finding cited from runs — assumes you already know which hypothesis to
write. In practice, real research starts further upstream: with an
*exploration* whose shape isn't yet narrow enough to be falsifiable.
Threads capture that exploration in the same `kb/` graph as everything
else, so it doesn't get lost in session notes (which are for
contextualization, not task delegation) or pushed off into external
trackers (which break the wikilink graph).

## When to use a thread vs. a hypothesis

| Situation | Artifact |
|---|---|
| You can write a single falsifiable claim with a test plan | `H###` |
| You can describe one experiment that produces evidence for/against a claim | `E###` (under an `H###`) |
| You're tracking a **forward-looking concern** that may spawn 2–5 hypotheses over time | `T###` |
| You're documenting *what just happened* in a session | session note (in `sessions/`, not `kb/`) |

Symptom that you should use a thread: you find yourself wanting to
write a hypothesis but the "## Test Plan" section feels forced because
you don't yet know what you'd test or how. The hypothesis form is
trying to push you to commit; the thread form lets you stay
exploratory while keeping the work durable and graph-linked.

## Lifecycle

```
PROPOSED → EXPLORING → PROMOTED   (one or more H### spawned; thread persists as parent context)
                    ↓
                    └─→ CLOSED    (decided not to pursue / out of scope / superseded)
```

- **`PROPOSED`** — the thread exists but no concrete work is underway
  to advance it. It's a parked idea. **Writing it down or
  occasionally thinking about it does NOT promote to `EXPLORING`.**
  A thread that was drafted months ago and hasn't seen activity stays
  `PROPOSED` indefinitely. That's fine — the status is a signal
  about *current* work, not historical intent. (Salvaged or
  backfilled threads should start here by default; the salvage
  itself doesn't count as exploration work.)
- **`EXPLORING`** — someone is actively running a baseline, reviewing
  literature toward a sub-question, pursuing a listed promotion
  criterion, or drafting the hypothesis language. There's a
  work-in-flight signal, not just intent. When the work pauses,
  manually demote back to `PROPOSED` — don't leave stale `EXPLORING`
  threads that falsely suggest active effort.
- **`PROMOTED`** — at least one `H###` has been spawned from the
  thread (via `aexp new-hypothesis --thread T###`). The thread
  persists as parent context for those hypotheses; it doesn't go
  away. Filling `## Conclusion` at PROMOTED-time is optional but
  useful as a pointer to the spawned H artifacts.
- **`CLOSED`** — explicitly closed without promoting. Decided not to
  pursue, turned out to be a non-issue, or superseded by another
  thread / hypothesis. Fill `## Conclusion` to record *why*.

Status transitions are manual (edit `status:` frontmatter or use
`aexp close-thread` for the close/promote transitions). aexp doesn't
auto-transition based on what's happened in the kb graph — implicit
state machinery is harder to reason about than a deliberate edit.
The common failure mode is over-promoting (calling a parked idea
`EXPLORING` because it *feels* active, not because it is). Default
to the stricter reading.

## Linkage to H/E/F

Threads are **not** in the H→E→F enforcement chain. The PreToolUse
hook (`enforce_hef_chain`) doesn't require a hypothesis to have a
parent thread — most hypotheses won't.

When a hypothesis IS spawned from a thread:

- The hypothesis's frontmatter records `thread: "T###"`.
- The thread's `## Links` section is auto-patched with `- [[H###]]`
  so `kb_validate`'s bidirectional-link check passes.
- The validator confirms the referenced thread exists on disk
  (`enforce_hef_chain` blocks the write otherwise).
- Findings cite hypotheses + experiments, not threads. The thread
  graph is reachable via `[[H###]]` from the finding's `## Links`
  section.

## Required template sections

When you create a thread via `aexp new-thread`, the rendered file
includes these top-level sections (the validator enforces every one
under `missing_template_header`):

- **`## Statement`** — the broad concern, broader than a single
  hypothesis. *"Investigate using the ontology graph structure at
  inference and evaluation time as a real architectural lever."*
- **`## Sub-questions`** — bullet list of candidate hypothesis stubs.
  Each one could plausibly become its own `H###` later. Refine over
  the thread's lifetime as the shape clarifies.
- **`## Promotion criteria`** — the prerequisites for spawning the
  first hypothesis. Empirical baselines, design decisions, external
  dependencies. Without a promotion criterion, threads drift into
  permanent "exploring" with no exit condition.
- **`## Open links`** — external references: papers, code paths,
  prior threads, sessions where this surfaced. Free-form Markdown
  links (not validated against the kb graph — that's `## Links`
  below).
- **`## Notes`** — running journal of thinking on this thread.
  Date-stamped entries recommended. **This is what distinguishes a
  thread from a session note** — the journal lives WITH the thread,
  not in a date-stamped file that rots.
- **`## Conclusion`** — filled when the thread closes (via
  `aexp close-thread` or hand-edit).
- **`## Links`** — wikilinks in the kb graph. Always includes
  `[[ACTIVE]]` and `[[CHALLENGE]]`; auto-patched with `[[H###]]`
  whenever a hypothesis is spawned from this thread.

You can add **extra** sections beyond these — the validator allows
extension, just not contraction. If a thread accumulates "## Anti-
patterns to avoid," "## Open questions for collaborators," etc., add
them; required sections still need to be present (with at least a
placeholder body).

## Command surface

### CLI

```
aexp new-thread    --title "..."  [--id T###]   [--link <wikilink>...]
aexp list-threads  [--status STATUS]  [--tag TAG]
aexp show-thread   <T###>
aexp close-thread  <T###>  [--conclusion "<markdown>"]  [--promoted]
aexp new-hypothesis --title "..." --thread T###  [--link <wikilink>...]
```

### MCP tools

`new_thread`, `list_threads`, `show_thread`, `close_thread`. Plus
`new_hypothesis` gains a `thread_id` parameter for promotion.

### Slash commands

`/aexp-new-thread`, `/aexp-list-threads`, `/aexp-show-thread`,
`/aexp-close-thread`. Promotion uses `/aexp-new-hypothesis` with the
`--thread` flag — there's no separate `/aexp-promote-thread` because
the workflow is just "create a hypothesis with a parent."

### Python API

```python
from aexp import (
    new_thread, close_thread, load_thread, list_kb_artifacts,
    new_hypothesis,
)

t = new_thread(title="hierarchy-aware scoring")
# ... fill in the body sections ...

# Later, promote:
h = new_hypothesis(
    title="sibling-score aggregation refines specificity",
    thread_id=t.artifact_id,
)
# Hypothesis is created with thread: T001 in frontmatter; T001's
# ## Links is patched with [[H001]] automatically.

# When the thread is fully promoted (all sub-questions are now
# spawned hypotheses or won't be), close it:
close_thread(
    t.artifact_id,
    conclusion="Spawned [[H001]], [[H002]]; thread persists as parent context.",
    new_status="PROMOTED",
)
```

## Section boundary: `## Sub-questions` vs. `## Promotion criteria`

These sections look similar but carry different weight, and edge
cases blur them. The distinction that holds up in practice:

- **`## Sub-questions`** lists the candidate *claims* this thread
  could spawn — each bullet reads as a future hypothesis stub. If a
  bullet says *"does technique X outperform baseline for rare
  labels?"*, that's a sub-question. Preconditions that are specific
  to *this individual sub-question* (e.g. "prerequisite: run a
  per-label AUROC baseline first") can live inline with the bullet
  — they're the sub-question's own setup, not a thread-wide gate.
- **`## Promotion criteria`** lists preconditions that must hold
  before *any* sub-question can be promoted to a hypothesis. This
  is thread-wide scaffolding: empirical baselines the whole thread
  depends on, external dependencies (clinician input, data access),
  methodological scoping that has to be locked before committing
  to a test plan.

Rule of thumb: *"prerequisite to any promotion → `## Promotion
criteria`; prerequisite to this specific sub-question → stays with
the sub-question."* When uncertain, put it in `## Sub-questions`
with prose that flags it as a prerequisite — moving up to
`## Promotion criteria` later is cheap.

## Cross-linked artifacts: create-then-link ordering

`kb_write_guard` runs `validate_kb` on every write; it rejects
writes with `links.missing_note` if a wikilink target doesn't exist
on disk. This trips up **cross-referenced artifact pairs** — e.g.
two threads that deliberately reference each other as "separate but
related" research directions.

**Pattern for cross-linked pairs:** create both skeletons first, then
populate their `## Links` sections. For two threads T001 and T002
that cross-reference:

```
# 1. Create both skeletons (default Links: [[ACTIVE]], [[CHALLENGE]]):
aexp new-thread --title "Thread A topic"   # writes T001 w/ minimal links
aexp new-thread --title "Thread B topic"   # writes T002 w/ minimal links

# 2. NOW edit each to add the cross-link (both targets exist on disk):
# Edit T001: add `- [[T002]]` to ## Links
# Edit T002: add `- [[T001]]` to ## Links
# Both writes pass validation — each references an existing artifact.
```

Same pattern applies to any cross-linked artifacts: e.g. an H that
references a superseded-by H that doesn't exist yet, or two findings
that cite each other. Writing the wikilink before the target exists
will be blocked; write-both-then-link works.

## Idioms

- **Naming.** Thread titles describe the *concern*, not the *answer*.
  "Hierarchy-aware scoring and evaluation" not "ontology graph fixes
  inference accuracy." If you can write the answer as a title, it's
  probably ready to be a hypothesis, not a thread.
- **Sub-question bullets become hypothesis titles later.** When you
  draft `## Sub-questions`, write each bullet as a sentence narrow
  enough to plausibly become an H1 heading on a future `H###`. This
  forces the right level of abstraction.
- **Notes vs. session notes.** If you're writing a journal entry
  about *what you thought today on the broader question*, that goes
  in the thread's `## Notes` section. If you're writing a journal
  entry about *what you did this session* (commands run, decisions
  made, blockers hit), that goes in `sessions/`. Both can reference
  each other via wikilinks; they aren't competing.
- **Don't conflate threads with epics.** A thread doesn't have a
  schedule, doesn't have a deliverable, isn't task-managed. It's a
  research direction. If you find yourself wanting to track tasks,
  use whatever issue tracker fits — threads aren't for that.
- **Default to PROPOSED on creation.** Salvaged drafts, backfilled
  threads, and threads added opportunistically during other work all
  start `PROPOSED`. Only promote to `EXPLORING` when you're actually
  running work — not when you *intend to* later.
