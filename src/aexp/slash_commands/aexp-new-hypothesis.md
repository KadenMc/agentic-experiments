---
description: "Create a new Limina hypothesis (H###) with a validator-clean skeleton."
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
3. Run:

   ```
   python -m aexp new-hypothesis --title "<title>" [--link <extra>] [--link <extra>]
   ```

   The command allocates the smallest unused ``H###``, writes
   ``kb/research/hypotheses/H###-<slug>.md`` from ``templates/hypothesis.md``,
   and reports the new id + path.

4. Open the new file and fill in the prose sections (``## Statement``,
   ``## Mechanism``, ``## Why This Might Generalize``, ``## Shortcut Risks``,
   ``## Test Plan``, ``## Evidence``) from what the user told you. The
   ``## Conclusion`` section is left blank for after testing. Do NOT edit
   the frontmatter, the blockquote metadata, or the ``## Links`` section —
   those are already correct.

5. Run `python -m aexp validate --kb-only` to confirm the new file is clean.
