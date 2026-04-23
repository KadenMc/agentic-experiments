---
description: "Create a Limina finding (F###) skeleton; add run citations separately."
---

Create a new finding artifact citing one hypothesis + one experiment. This
command writes the skeleton and patches both parents' ``## Links`` sections
with ``- [[F###]]`` — but it does NOT populate ``supporting_runs:``. For
finding a specific run / batch to cite, prefer `/aexp-close-run` or
`/aexp-close-batch` — they're the opinionated flow for close-out.

Use `/aexp-new-finding` when you want a finding placeholder that isn't
yet tied to concrete runs (e.g. synthesis findings combining prior
findings, or a paper-framing finding you'll wire up later).

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
   ``## Evidence``, ``## What Improved For Real``, ``## Remaining Debt``,
   ``## Next Move``). Do not edit the ``## Links`` section.

4. Run `python -m aexp validate` to confirm. If you plan to cite concrete
   runs, add ``supporting_runs:`` to the frontmatter as a list of mappings
   (see the close-run / close-batch docs for the exact schema), then
   re-validate.
