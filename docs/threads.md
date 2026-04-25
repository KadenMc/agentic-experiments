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

- **`PROPOSED`** — created, not yet being actively explored.
- **`EXPLORING`** — actively being explored. Set this manually (edit
  the `status:` frontmatter) when work begins.
- **`PROMOTED`** — at least one `H###` has been spawned from the
  thread (via `aexp new-hypothesis --thread T###`). The thread
  persists as parent context for those hypotheses; it doesn't go
  away.
- **`CLOSED`** — explicitly closed without promoting. Decided not to
  pursue, turned out to be a non-issue, or superseded by another
  thread / hypothesis.

Status transitions are manual (edit frontmatter or use
`aexp close-thread`). aexp doesn't auto-transition based on what's
happened in the kb graph — implicit state machinery is harder to
reason about than a deliberate edit.

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
