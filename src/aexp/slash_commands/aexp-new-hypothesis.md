---
description: "Create a new hypothesis (H###) with a validator-clean skeleton."
---

Create a new hypothesis artifact. `aexp` handles id allocation, template
rendering, and the ``## Links`` block — you do NOT need to write the markdown
by hand or remember the alias / frontmatter rules.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

Flow:

1. Ask the user for the hypothesis title — a short falsifiable claim, e.g.
   *"An ontology-backed reasoning layer prevents confident wrong ECG
   interpretations from a tool-using LLM."* Keep it under ~80 characters;
   it will become both the H1 heading and the filename slug.
2. Ask whether there are any extra targets to include in the ``## Links``
   block (rare — usually empty; skip unless the user mentions e.g.
   *"this supersedes H003"*). ``ACTIVE`` and ``CHALLENGE`` are always added
   automatically.
3. Optional: if this hypothesis is being **promoted from a thread**
   (``T###``), pass ``--thread T###``. The thread must already exist
   on disk; its ``## Links`` section will be auto-patched with
   ``- [[H###]]`` and the H frontmatter will record
   ``thread: T###``. After successful creation, consider running
   `/aexp-close-thread <T###> --promoted` if the thread should now
   transition from ``EXPLORING`` to ``PROMOTED``.

4. Run:

   ```
   python -m aexp new-hypothesis --title "<title>" \
       [--thread <T###>] [--link <extra>] [--link <extra>]
   ```

   The command allocates the smallest unused ``H###``, writes
   ``kb/research/hypotheses/H###-<slug>.md`` from the shipped
   template, and reports the new id + path.

5. Open the new file and fill in the prose sections from what the user
   told you:
   - ``## Statement``, ``## Mechanism``, ``## Why This Might Generalize``,
     ``## Shortcut Risks``, ``## Evidence`` — straightforward.
   - ``## Test Plan`` — the template ships two sub-blocks (pre-registered
     vs. exploratory). **Pick the framing that's actually true** and
     delete the other block. **Don't fabricate retroactive
     confirm/reject thresholds for runs that were exploratory.** That's
     the dishonesty trap the dual-mode template is designed to surface
     — it caught Kaden's first author here on 2026-04-24.
   - Leave ``## Conclusion`` blank for after testing.
   Do NOT edit the frontmatter, the blockquote metadata, or the
   ``## Links`` section — those are already correct.

6. Run `python -m aexp validate --kb-only` to confirm the new file is
   clean. The validator now checks every shipped template header is
   present (`missing_template_header` issue code) — fill a placeholder
   rather than deleting a section.
