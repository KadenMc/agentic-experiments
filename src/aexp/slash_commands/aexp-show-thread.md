---
description: "Show one thread's frontmatter + body summary."
---

Display a single thread artifact's metadata and key sections.
Read-only.

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user for the ``T###`` id. If they don't know, run
   `/aexp-list-threads` first.

2. Run:

   ```
   python -m aexp show-thread <T###>
   ```

   For full body content, the agent can `Read` the file at the
   reported path directly.

3. If the thread has linked hypotheses (visible in the body's
   ``## Links`` section), suggest running
   `/aexp-show-run` (for runs of any spawned ``E###``) or just
   `Read`ing the H### markdown for the claim-level state.

4. If the thread is overdue for a status update — e.g. ``EXPLORING``
   for several weeks with no spawned hypotheses — flag that the
   user might want to either promote (via `/aexp-new-hypothesis
   --thread T###`) or close (`/aexp-close-thread`).
