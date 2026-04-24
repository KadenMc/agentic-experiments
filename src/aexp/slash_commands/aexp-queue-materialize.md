---
description: "Emit a runner script (shell / slurm / manual) from the queue."
---

Turn the pending-run queue into a runner script. The script is what
*actually executes* the jobs — aexp itself doesn't run anything here,
so the script can be submitted wherever the runtime env lives (local
machine, HPC cluster, separate container).

> **Invocation note.** Use `python -m aexp <verb>`. If your `python`
> doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`, field
> `conda_env_name`) or use the absolute `python_exe` from the same
> marker.

Flow:

1. Confirm the user's filter — usually a `--tag` that corresponds to
   one batch of queued work. `python -m aexp queue list --tag <tag>`
   first to verify the count matches expectations.

2. Ask what runner fits their environment:
   - **`shell`** (default) — sequential bash script. Good for local
     runs or simple single-node cluster jobs. Output usually `run.sh`.
   - **`slurm`** — emits a **starter template** with
     `#SBATCH --array=0-N` and a call to `aexp queue run --index
     "$SLURM_ARRAY_TASK_ID"`. Because aexp has no visibility into the
     user's cluster (partition, account, module loads, env activation,
     container setup), the template has `# TODO` placeholders the user
     must fill in. Ask for `--slurm-time` (e.g. `04:00:00`),
     `--slurm-mem` (e.g. `32G`), `--slurm-gpus`, `--slurm-partition`,
     and `--slurm-account` up-front to pre-fill what you can.

     **Often the better move** is to skip `materialize --runner slurm`
     entirely and have the user add one line to their existing working
     batch script:
     ``aexp queue run --tag <tag> --index "$SLURM_ARRAY_TASK_ID"``.
     If they already have a slurm script that works for their site,
     suggest this first; only generate a new template when they don't.
   - **`manual`** — plain list of `aexp run-queued <id>` lines, no
     shebang, no control flow. Useful when the user has a different
     job-runner (qsub, LSF, Airflow, etc.) and wants to splice the
     commands in themselves.

3. Run:

   ```
   # Shell (local / simple remote)
   python -m aexp queue materialize \
       --runner shell --output run.sh \
       [--tag <tag>] [--experiment <E###>]

   # Slurm (cluster)
   python -m aexp queue materialize \
       --runner slurm --output overnight.sbatch \
       --tag <tag> \
       --slurm-time 04:00:00 --slurm-mem 32G --slurm-gpus 1 \
       [--slurm-partition gpu] [--slurm-account team-x]

   # Manual (paste into your own runner)
   python -m aexp queue materialize \
       --runner manual --output commands.txt [--tag <tag>]
   ```

4. Print the output path and the suggested follow-up command:
   - `shell` → `bash <output>`
   - `slurm` → `sbatch <output>`
   - `manual` → "copy the lines into your runner of choice."

5. Remind the user the materialized script is **idempotent** —
   `aexp run-queued <id>` skips jobs already in a terminal state. If
   the user wants to re-run a failed job after fixing the training
   code, suggest `aexp run-queued <id> --force`.

6. If the user is running across machines, nudge them to commit the
   `.runs/` directory before and after execution so both sides see
   consistent state. Details in `docs/queue.md` (cross-machine sync
   workflow).
