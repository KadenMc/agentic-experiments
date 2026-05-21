---
description: "Iterate on a JupyterLab cell with the user via the Jupyter MCP bridge (read → propose → execute)."
---

Iterate on a notebook cell with the user, through the Jupyter MCP bridge.

> **Prerequisite.** This command requires the `mcp__jupyter__*` tool
> family — in particular `connect_to_jupyter`, `execute_code`,
> `read_cell`, and `execute_cell`. Those tools come from
> `aexp install --with-jupyter` plus a JupyterLab process reachable
> through the user's SSH tunnel. If the tools are missing, run `/mcp` to
> inspect server status and consult `docs/setup/jupyter-mcp.md` for the
> setup recipe.

Run through these steps:

1. **Check tool availability.** Verify that `mcp__jupyter__execute_cell`
   and `mcp__jupyter__read_cell` are present in your tool list. If not,
   stop and report:
   "Jupyter MCP integration not available in this session. Run
   `aexp install --with-jupyter`, ensure the SSH tunnel to the cluster is
   open, connect with `/aexp-jupyter-connect`, and restart Claude. See
   `docs/setup/jupyter-mcp.md`."

2. **Confirm session identity.** Before touching any cells, dispatch:
   ```
   execute_code(code="from aexp.jupyter import init; import json; print(json.dumps(init().model_dump(), default=str))")
   ```
   on the connected Jupyter. Pass the stdout to
   `mcp__aexp__jupyter_parse_introspection` and review the parsed
   `SessionInfo`. Confirm with the user:
   - the SLURM job (if any) and host are what you expect;
   - the `attached_notebooks` list contains the notebook the user is
     actually working in;
   - no other busy kernel on the same host is holding GPU memory you
     shouldn't disturb.

   If anything mismatches — wrong SLURM job, wrong host, unexpected GPU
   resident — STOP and ask. Do not proceed to step 3. To switch to a
   different Jupyter, use `/aexp-jupyter-connect`.

3. **Identify the target notebook and cell.** This single-server setup
   has no live "what is the user looking at" tool, so ask the user
   directly:
   "Which notebook should I work in, and which cell — give me the cell
   index, or describe it (e.g. 'the training loop')?"
   Cross-check the notebook name against the `attached_notebooks` list
   from step 2. Open it with `use_notebook` if it isn't already open.

4. **Locate and quote the cell.** Use `read_cell(cell_index=N)` for a
   single cell, or `read_notebook` (brief mode) to find the cell the
   user described. Report the notebook path, cell index, and cell type,
   and quote the source verbatim so the user can confirm you're
   targeting the right cell before any edit.

5. **Gather context if needed.** If the user's request references "the
   cells above" or relies on prior state, use `read_cell` on adjacent
   indices, or `read_notebook` (brief mode) for an overview. Don't dump
   the whole notebook unless asked.

6. **Propose a change.** Describe what you intend to modify and why.
   Do NOT make the edit until the user confirms.

7. **On approval, apply the edit.** Use:
   - `edit_cell_source` for surgical find/replace within one cell.
   - `overwrite_cell_source` for full replacement.
   - `insert_cell` to add a new cell.

8. **Execute the cell.** Call `execute_cell(cell_index=N)` with a
   reasonable timeout. Paste the actual stdout / errors verbatim — don't
   paraphrase.

9. **Iterate or wrap up.** If the cell errored, propose the next fix and
   loop back to step 6. If it succeeded, ask the user whether to
   continue or stop.

> **Multi-cell runs.** To run a span of cells, loop
> `execute_cell(cell_index=i)` over the indices in order.
