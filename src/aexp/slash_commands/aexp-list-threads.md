---
description: "List research threads, optionally filtered by status or tag."
---

Print every thread under ``kb/research/threads/`` with its current
lifecycle status. Read-only.

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user what slice they want (or default to everything):
   - `--status <state>` — filter to one of ``PROPOSED``,
     ``EXPLORING``, ``PROMOTED``, ``CLOSED``.
   - `--tag <tag>` — match against the ``tags`` frontmatter list.

2. Run:

   ```
   python -m aexp list-threads [--status <state>] [--tag <tag>]
   ```

3. Summarize: count by status, highlight any threads in ``EXPLORING``
   that the user might have lost track of. If the user is preparing to
   start work, suggest filtering by ``--status EXPLORING`` to see what's
   already in flight.

4. For details on a single thread, follow up with
   `/aexp-show-thread T###`.
