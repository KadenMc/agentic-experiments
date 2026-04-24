---
description: "Create a new Limina experiment (E###) under an existing hypothesis."
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
   - **Title** — short, e.g. *"Aligned 32-ECG ablation across full vs
     classify-only"*. Becomes the H1 heading + filename slug.
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

3. Open the new file and fill in the prose sections (``## Objective``,
   ``## Setup``, ``## Procedure``, ``## Expected Outcome``, ``## Progress``).
   Leave ``## Results``, ``## Analysis``, and ``## Decision`` blank — those
   get populated as runs produce data and you write a finding via
   `/aexp-finding-from-run` / `/aexp-finding-from-batch`.

4. If the user is ready to register runs now, call `/aexp-new-run` next —
   it wants the ``E###`` id you just created.

5. Run `python -m aexp validate --kb-only` to confirm both the new file
   and the patched parent validate clean.
