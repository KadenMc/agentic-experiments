---
description: "Create a Limina finding (F###) with no run citations yet (synthesis / deferred)."
---

Create a new finding skeleton that cites a hypothesis + experiment but has
no concrete ``supporting_runs:`` yet. Use this when you want the finding
artifact on disk — linked into the H→E→F graph, backlinks patched — but
the run citations will be added later (synthesis findings, findings that
cite other findings, paper-framing findings wired to runs after the fact).

> **Three sibling commands create findings** — pick by what the finding
> cites:
>
> - **`/aexp-finding-placeholder`** (this command) — no run citations yet
> - **`/aexp-finding-from-run`** — cites one specific signac job
> - **`/aexp-finding-from-batch`** — cites an `(experiment, condition)`
>   batch selector
>
> All three write the F### skeleton, patch the parent H + E backlinks,
> and leave you to fill in the prose sections.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user for the title, parent ``H###``, parent ``E###``, and impact
   level (``CRITICAL`` | ``HIGH`` | ``MEDIUM`` | ``LOW``; default
   ``MEDIUM``). Both parents must exist on disk.
2. Run:

   ```
   python -m aexp new-finding --title "<title>" \
       --hypothesis <H###> --experiment <E###> [--impact HIGH]
   ```

   Output reports the new ``F###``, its path, and which parent files were
   patched with the backlink.

3. Open the new file and fill in the prose sections (``## Finding``,
   ``## Evidence``, ``## Caveats``, ``## What Improved For Real``,
   ``## Remaining Debt``, ``## Next Move``). Do not edit the
   ``## Links`` section. Boundary reminder: ``## Caveats`` is what
   limits *interpretation* of this finding (small sample, domain shift,
   instrumentation gaps); ``## Remaining Debt`` is what's still a
   workaround in the *system*. Both load-bearing, both required.

4. Run `python -m aexp validate` to confirm. When you're ready to cite
   concrete runs, either (a) re-invoke via `/aexp-finding-from-run` /
   `/aexp-finding-from-batch` for a new F###, or (b) hand-edit this
   F###'s frontmatter to add ``supporting_runs:`` as a list of mappings
   (see the from-run / from-batch docs for the exact schema), then
   re-validate.
