---
description: "Transition a thread to CLOSED or PROMOTED; fill the ## Conclusion section."
---

Close out a thread artifact. Two distinct close paths:

- **CLOSED** (default) — the thread has been decided against, was
  superseded, or turned out to be a non-issue. The thread persists in
  ``kb/research/threads/`` as a historical record but is no longer
  active research direction.
- **PROMOTED** — one or more hypotheses have been spawned from this
  thread, and the thread now serves as parent context for them. The
  thread persists indefinitely; closing it as ``PROMOTED`` is a status
  signal, not a deletion.

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user:
   - **Which thread** (T### id). Confirm it exists with
     `/aexp-show-thread T###`.
   - **Which status to apply** — CLOSED (default) or PROMOTED.
     Disambiguate by asking "did this spawn one or more hypotheses?"
     If yes, PROMOTED; if no, CLOSED.
   - **Conclusion text** — markdown body to write into the thread's
     ``## Conclusion`` section. For CLOSED, state why the thread was
     dropped. For PROMOTED, briefly point at the spawned ``H###``
     artifacts and any context that doesn't naturally live on those
     hypotheses.

2. Run:

   ```
   # Default: CLOSED
   python -m aexp close-thread <T###> --conclusion "<markdown body>"

   # Or: PROMOTED
   python -m aexp close-thread <T###> --promoted --conclusion "<markdown body>"
   ```

   Output reports the path and the new status.

3. Run `python -m aexp validate --kb-only` to confirm the thread
   still validates (the rewrite preserves all required template
   headers; only ``status``, ``last_updated``, and ``## Conclusion``
   change).

4. If the user picked PROMOTED, the spawned ``H###`` files should
   already have ``thread: T###`` in their frontmatter (set at
   creation time via `aexp new-hypothesis --thread T###`). Verify
   with `aexp validate` — backlinks should round-trip cleanly.
