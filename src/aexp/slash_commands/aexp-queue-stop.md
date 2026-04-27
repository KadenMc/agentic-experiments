---
description: "Interrupt a running queued job (the live `aexp run-queued <id>` subprocess)."
---

Stop a queued job that is currently executing under
`aexp run-queued`. Sends SIGTERM to the runner's process group, polls
during a grace window, then escalates to SIGKILL if needed. The job
transitions to status `stopped` (distinct from `failed` / `abandoned`)
so post-hoc forensics can tell operator-stops from real failures.

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Ask the user which job to stop:
   - Job id (full or short prefix). They can list candidates first via
     `/aexp-queue-list` and copy a short id.

2. Optionally ask about flags:
   - `--grace-s <seconds>` — how long to wait between SIGTERM and
     SIGKILL. Default 5. Lower for fast-iteration sessions; raise for
     runners that flush state on shutdown.
   - `--force` — skip SIGTERM, send SIGKILL immediately. Use only when
     a graceful shutdown is known not to work (the runner ignores
     SIGTERM, or you need the kernel to free GPU memory now).

3. Run:

   ```
   python -m aexp queue stop <jobid> [--grace-s 5] [--force]
   ```

4. Confirm what landed: the job status should now be `stopped` and
   `job.doc["queue"]["last_error"]` should record `cause: operator_stop`
   plus the kill mechanism (SIGTERM, SIGKILL, or "no live process
   recorded").

5. **Host scope reminder.** `queue stop` only works on the machine
   that started the run — pids are local. If the user is on a
   different login node than the one that fired `aexp run-queued`,
   the verb refuses with a clear "this job started on host X" error.
   The right answer is to ssh into the recording host and run the
   verb there.

6. **PID-recycle safety.** On Linux, the verb fingerprints the
   recorded process's start time and refuses to send signals if the
   pid has been recycled to an unrelated process. Cross-platform
   correctness: status still transitions, no kill is attempted.
