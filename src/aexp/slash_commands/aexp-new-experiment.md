---
description: "Create a new experiment (E###) under an existing hypothesis."
---

Create a new experiment artifact under an existing hypothesis. ``aexp``
handles id allocation, template rendering, the ``## Links`` block, AND
patches the parent hypothesis's ``## Links`` section with ``- [[E###]]`` so
``kb_validate`` passes without a second edit.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

Flow:

1. Ask the user for:
   - **Title** — short, e.g. *"Aligned 32-sample ablation across full vs
     retrieval-only"*. Becomes the H1 heading + filename slug.
   - **Parent hypothesis** — the ``H###`` this experiment tests. If the user
     doesn't know, run
     `python -m aexp validate --kb-only` (or ``ls kb/research/hypotheses/``)
     to show them the options. If the hypothesis doesn't exist yet, bail and
     run `/aexp-new-hypothesis` first — ``enforce_hef_chain`` will block an
     orphan experiment.
2. Run:

   ```
   python -m aexp new-experiment --title "<title>" --hypothesis <H###>
   ```

   Output reports the new ``E###``, its path, and which parent file was
   patched with the backlink.

3. Open the new file and fill in the prose sections:
   - **`## Objective`**, **`## Setup`**, **`## Procedure`** — the up-front
     plan.
   - **`## Caveats`** — known limitations, instrumentation gaps, deviations.
     Be honest; ``_None._`` is fine for fully-instrumented runs but most
     experiments accumulate something. **Don't bury caveats under
     `## Setup` or another section** — top-level visibility is the point.
   - **`## Intent`** — pick the framing that fits and **delete the
     non-applicable block** (template ships both):
     - *Pre-registered* (paper-cited / high-stakes work): confirm/reject
       criteria with thresholds.
     - *Exploratory / smoke test*: a one-line purpose. **Don't fabricate
       retroactive thresholds for smoke tests** — that's the trap the
       template is designed to surface.
   - **`## Progress`** — what's been run / completed so far. Blank at
     experiment-creation time.
   - Leave **`## Results`**, **`## Outcome Summary`**, **`## Decision`**
     blank initially — those get populated as runs produce data.

4. Section boundary — `## Outcome Summary` (E) vs Finding prose (F).
   The experiment's `## Outcome Summary` reports *what happened in this
   specific run*. Generalizable claims — what the result means beyond
   this run — belong in the linked **Finding's** prose, written via
   `/aexp-finding-from-run` or `/aexp-finding-from-batch`. If no finding
   has been authored yet, `## Outcome Summary` can carry "what we
   learned" until one does. The experiment's `## Decision` is for
   experiment-level next-actions (re-run, abandon, follow-up E); the
   finding's `## Next Move` is for claim-level next-actions (ship, scale
   up replication).

5. If the user is ready to register runs now, call `/aexp-new-run` next —
   it wants the ``E###`` id you just created.

6. Run `python -m aexp validate --kb-only` to confirm both the new file
   and the patched parent validate clean. The validator now checks that
   every shipped template header is present (`missing_template_header`
   issue code) — fill placeholders rather than deleting sections.
