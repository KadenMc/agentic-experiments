---
description: "Create a new Limina thread (T###) — forward-looking research concern broader than a hypothesis."
---

Create a thread artifact. **Threads are not hypotheses.** A hypothesis
is a falsifiable claim with a test plan; a thread is the surrounding
*exploration* that motivates choosing which hypotheses to write later.
A thread typically spawns 2–5 ``H###`` over its lifetime, then either
transitions to ``PROMOTED`` (parent context for those hypotheses) or
``CLOSED`` (decided not to pursue). See ``docs/threads.md`` for the
full model.

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

Flow:

1. Confirm a thread is the right artifact. Three checks:
   - **Is the user describing one falsifiable claim?** If yes, this is
     an ``H###`` — use `/aexp-new-hypothesis`. Threads are for the
     case where multiple plausible hypotheses haven't been narrowed
     down yet.
   - **Is the user describing run-level methodology / a single
     experiment?** If yes, that's an ``E###`` — needs a parent
     ``H###`` first.
   - **Is the user describing a forward-looking concern, question, or
     research direction that they want to track but isn't yet
     concrete?** That's a thread.

2. Ask the user for:
   - **Title** — short noun phrase, e.g. *"Hierarchy-aware scoring and
     evaluation"*. Becomes the H1 heading + filename slug.
   - Optional **extra links** — wikilinks to related artifacts (prior
     threads this builds on, hypotheses already in scope, etc.).
     ``ACTIVE`` and ``CHALLENGE`` are auto-added.

3. Run:

   ```
   python -m aexp new-thread --title "<title>" [--link <extra>] [--link <extra>]
   ```

   Output reports the new ``T###`` and its path under
   ``kb/research/threads/``.

4. Open the new file and fill in the prose sections. The validator
   checks every shipped template header is present
   (``missing_template_header`` issue code) — fill placeholders
   rather than deleting sections. Required sections:
   - **`## Statement`** — the broad question / concern, broader than
     a single hypothesis.
   - **`## Sub-questions`** — bullet list of candidate hypothesis
     stubs. Each one could plausibly become its own ``H###`` later.
   - **`## Promotion criteria`** — when does this thread spawn
     hypotheses? What are the prerequisites (empirical baselines,
     design decisions, external dependencies)? The point is to keep
     the thread from drifting into permanent "exploring" with no
     exit condition.
   - **`## Open links`** — external references: papers, code paths,
     prior threads, sessions where this surfaced. Free-form Markdown
     links, not validated.
   - **`## Notes`** — running journal. **This is what distinguishes a
     thread from a session note** — the journal lives WITH the
     thread, not in a date-stamped file that rots. Date-stamped
     entries within this section are recommended.
   - **`## Conclusion`** — leave as the placeholder; filled later via
     `/aexp-close-thread`.

5. Run `python -m aexp validate --kb-only` to confirm clean.

6. Next step: when the user is ready to spawn a hypothesis from this
   thread, they call `/aexp-new-hypothesis` with the ``--thread T###``
   flag. The thread's status should also be advanced manually (edit
   the frontmatter) from ``PROPOSED`` to ``EXPLORING`` once active
   work begins.
