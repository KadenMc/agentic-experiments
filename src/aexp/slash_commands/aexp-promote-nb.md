---
description: "Promote working notebook cells into a tracked-run-ready Python script under experiments/ wrapped with aexp.tracked_run."
---

Promote a working set of cells from a JupyterLab notebook into a
tracked-run script — the load-bearing transition from exploration to
committed experiment.

> **When to use this.** You've iterated on cells with the user (probably
> via `/aexp-jupyter-iterate`), the pipeline works end-to-end on a smoke
> test, and you're ready to lift the working code out of the notebook
> and into a script that can be queued, swept, and cited in a finding.
> The notebook stays as the smoke-test record — it's not edited, only
> read. Outputs land at `<repo_root>/experiments/E<id>-<slug>.py`.

> **Prerequisite.** Best with the `mcp__jupyter__*` tools available
> (from `aexp install --with-jupyter`). If they're not, you can still
> promote cells from a `.ipynb` file on disk via the standard Read tool;
> everything in this command works either way.

> **Invocation note.** The examples below use `python -m aexp` directly.
> If running from a Claude Code session where `python` does not resolve
> to the env that has `aexp` installed, prefix with the env selector:
>
> - **conda users**:
>   `conda run -n <env-name> python -m aexp <verb>`
>   The env name was captured at install time — see
>   `.aexp/installed.json` field `conda_env_name`.
> - **venv users**:
>   `"<python_exe>" -m aexp <verb>`, where `<python_exe>` is the absolute
>   path from `.aexp/installed.json` field `python_exe`.

Run through these steps:

1. **Tool availability check.** Verify whether the `mcp__jupyter__*`
   tools (`read_cell`, `read_notebook`, `use_notebook`) are present. If
   yes, use them in the steps below. If not, ask the user for the path
   to the `.ipynb` file on disk and read it via the standard Read tool —
   JupyterLab notebooks are JSON; you can extract `cells[i].source`
   directly.

2. **Identify the source notebook.** Ask the user for the notebook path
   explicitly. With the MCP bridge, cross-check it against the open
   notebooks (`aexp.jupyter.init().attached_notebooks`) and open it with
   `use_notebook` if it isn't already open. Without the bridge, take the
   path the user gives you.

3. **Identify the cell range to promote.** Ask:
   "which cells should I promote — give me indices (e.g., `4-12`) or
   describe the cells (e.g., 'from the model definition through the
   training loop')." Read each target cell (`read_cell(cell_index=N)`
   via MCP, or by indexing into `cells[]` from the on-disk JSON). Quote
   the source verbatim back to the user and confirm the selection before
   going further.

4. **Identify the experiment.** Ask which `E###` this script is being
   promoted under. If the user is unsure, suggest checking
   `kb/ACTIVE.md` for in-flight experiments or running
   `python -m aexp list-runs --experiment <E###>` to inspect what
   already exists. **Refuse to proceed without a real experiment ID** —
   promotion without a hypothesis/experiment chain is the failure mode
   this command exists to prevent. If the experiment doesn't exist
   yet, point the user at `/aexp-new-experiment` and stop here.

5. **Identify parameterization candidates.** Scan the promoted cells for
   top-level assignments of literal values that look like hyperparams.
   Common shapes:
   - `lr = 1e-4`, `learning_rate = 3e-5`
   - `seed = 0`, `random_state = 42`
   - `batch_size = 32`, `n_epochs = 10`
   - `model_name = "facebook/..."`, `dataset_split = "train"`
   - `condition = "full"` or similar mode flags
   Propose a list to the user. Get their approval, removals, and any
   additional knobs they want exposed. These become `job.sp` lookups in
   the generated script.

6. **Choose the target path.** Default to
   `<repo_root>/experiments/E<id>-<slug>.py`, with the slug derived
   from the experiment artifact's title (kebab-case, ASCII). Let the
   user override per invocation. Refuse to overwrite an existing file
   without explicit confirmation. Refuse paths outside the repo root.

7. **Generate the script.** Use this template, filling in the
   `<placeholders>` from steps 2-6:

   ```python
   """<title from experiment artifact>.

   Tracked-run script for experiment <E###>. Promoted from
   <notebook_path> cells <range> by /aexp-promote-nb on <YYYY-MM-DD>.

   Run via the queue:
       python -m aexp queue add --experiment <E###> --sp <key>=<val>,...
       python -m aexp queue materialize --runner shell --output run.sh
       bash run.sh
   """
   from __future__ import annotations

   import argparse
   import sys

   from aexp import open_run, tracked_run


   def main(job_id: str) -> int:
       job = open_run(job_id)
       sp = dict(job.sp)

       # === Promoted cells <range> from <notebook_path> ===

       # Imports (lifted to module top in the body block where Python
       # syntax requires; otherwise repeated here for explicitness).
       <imports from promoted cells>

       # Parameterized via job.sp (see /aexp-promote-nb step 5):
       <param_a> = sp.get("<param_a>", <sensible_default>)
       <param_b> = sp.get("<param_b>", <sensible_default>)
       # ... one line per identified knob ...

       # Body (promoted cells with parameterized literals replaced).
       with tracked_run(job, project="<project>", offline=True) as run:
           <body of promoted cells>
           # run.log({"<metric>": <value>}) where the original cells
           # printed metrics or stored them in scalars.

       return 0


   if __name__ == "__main__":
       parser = argparse.ArgumentParser()
       parser.add_argument("job_id", help="signac job id (32 hex)")
       args = parser.parse_args()
       sys.exit(main(args.job_id))
   ```

   Notes when filling the template:
   - Hoist imports cleanly to module top (right under the existing
     `from aexp import ...`).
   - Keep the `tracked_run` block tight — only the actual training /
     evaluation code that needs the W&B run handle goes inside.
     Configuration, model construction, and dataset loading typically
     happen *outside* the `with` so partial setup isn't billed against
     the wandb run.
   - For `project="..."`: check the experiment frontmatter for a
     `wandb_project:` or similar key; otherwise ask the user.
   - `offline=True` is the safe default for HPC compute nodes without
     internet — the user can run `python -m aexp sync-offline` later.
     If they're on a node with internet, drop `offline=True`.

   Use the Write tool to create the file. Refuse to write outside the
   repo root.

8. **Optionally update the experiment's `runner_command:` frontmatter.**
   The frontmatter key is documented at
   `templates/experiment.md` lines 10-16. Suggested value:

   ```yaml
   runner_command: "python experiments/E<id>-<slug>.py {job_id}"
   ```

   This makes `python -m aexp run-queued <job_id>` (and therefore
   `python -m aexp queue run`) invoke the new script automatically.
   **Ask the user before editing the artifact** — it's a tracked KB
   file and the `PreToolUse` hook will validate the write.

9. **Print next steps.** Show the user the exact commands to register
   tracked runs:

   ```
   python -m aexp queue add --experiment <E###> --sp <key>=<val>,...
   python -m aexp queue materialize --runner shell --output run.sh
   bash run.sh
   ```

   If the user identified more than one parameterization knob, also show
   a sweep example:

   ```
   python -m aexp queue add --experiment <E###> --sweep "<param_a>=val1|val2,seed=0..3"
   ```

   Remind them: the source notebook is unchanged. It stays as the
   smoke-test record. If they want a marker in the notebook noting
   where the promoted code now lives, they can add a markdown cell
   themselves — this command does NOT edit the notebook.

> **Do NOT** invent or call a `tracked_notebook_run` API — there isn't
> one. The notebook → script transition is deliberately the moment when
> aexp's tracking discipline kicks in; there is no in-place tracked
> execution path for notebooks, by design.
