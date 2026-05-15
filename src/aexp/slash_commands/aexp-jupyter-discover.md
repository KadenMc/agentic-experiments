---
description: "List every Jupyter server the user has running, with port, attached notebook, SLURM job, and GPU residents."
---

Enumerate every Jupyter server the user has running that is visible from
the currently connected kernel.

> **Prerequisite.** This command requires an already-connected Jupyter MCP
> session (`mcp__jupyter*__execute_code` available). If not, run
> `/aexp-jupyter-connect` first or follow `docs/setup/jupyter-mcp.md`.

Run through these steps:

1. **Confirm tool availability.** Check that
   `mcp__jupyter*__execute_code` is in your tool list. If not, stop and
   ask the user to connect to a Jupyter via `connect_to_jupyter` (or
   `/aexp-jupyter-connect`) first.

2. **Dispatch the discovery snippet.** On the currently connected Jupyter,
   call:
   ```
   execute_code(code="from aexp.jupyter import init, describe_server; import json; info = init(); print(json.dumps({'self': info.model_dump(), 'siblings_described': [{**s.model_dump(), 'describe': describe_server(s.url, s.token)} for s in info.cluster_siblings]}, default=str))")
   ```

3. **Parse the output.** Pass the stdout to
   `mcp__aexp__jupyter_parse_introspection` (it handles the `self`
   subtree); for the `siblings_described` subtree, parse the JSON
   directly — each entry has `url`, `port`, `pid`, `hostname`, `token`,
   plus a `describe` block with `attached_notebooks` and `kernels`.

4. **Render a table for the user.** One row per Jupyter the user is
   running, including the current one (highlighted). Columns:
   `host`, `port`, `slurm_job` (or `-`), `attached_notebooks`,
   `gpu_used_by`, `kernel_state`. Quote notebook paths verbatim.

5. **Surface anything suspicious.** Call out:
   - any sibling with a busy kernel + non-zero GPU memory residents
     (likely active training — see "do not touch" caveat below);
   - any sibling whose `attached_notebooks` is empty AND `pid` differs
     from a user-launched process (could be stale).

> **Caveat.** Discovery shows what's running; it does not encode the
> user's *intent*. If the user previously said "don't touch the training
> kernel," honor that for the rest of this conversation regardless of
> what discovery surfaces. There is no persistent policy file in v1.

Do NOT call `connect_to_jupyter` from this command — listing is
read-only. If the user wants to switch, follow up with
`/aexp-jupyter-connect`.
