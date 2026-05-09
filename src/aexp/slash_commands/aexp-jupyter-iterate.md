---
description: "Iterate on a JupyterLab cell with the user via the Jupyter MCP bridge (read → propose → execute)."
---

Iterate on whatever cell the user is currently looking at in JupyterLab,
through the Jupyter MCP bridge.

> **Prerequisite.** This command requires the `mcp__jupyter-compute__*`
> tool family. Those tools come from `aexp install --with-jupyter` plus a
> JupyterLab process reachable through the user's SSH tunnel. If the
> tools are missing, run `/mcp` to inspect server status and consult
> `docs/setup/jupyter-mcp.md` for the cluster-side setup recipe.

Run through these steps:

1. **Check tool availability.** Verify that
   `mcp__jupyter-compute__notebook_get-selected-cell` and
   `mcp__jupyter-compute__execute_cell` are present in your tool list. If
   not, stop and report:
   "Jupyter MCP integration not available in this session. Run
   `aexp install --with-jupyter`, ensure the SSH tunnel to the cluster is
   open, and restart Claude Desktop. See `docs/setup/jupyter-mcp.md`."

2. **Identify what the user is looking at.** Call
   `mcp__jupyter-compute__notebook_get-selected-cell` to read the live UI
   selection. Report the notebook path, cell index, and cell type, and
   quote the source verbatim so the user can confirm you're targeting the
   right cell.

3. **Gather context if needed.** If the user's request references "the
   cells above" or relies on prior state, use
   `mcp__jupyter-compute__read_cell` on adjacent indices, or
   `mcp__jupyter-compute__read_notebook` (brief mode) for an overview.
   Don't dump the whole notebook unless asked.

4. **Propose a change.** Describe what you intend to modify and why.
   Do NOT make the edit until the user confirms.

5. **On approval, apply the edit.** Use:
   - `edit_cell_source` for surgical find/replace within one cell.
   - `overwrite_cell_source` for full replacement.
   - `insert_cell` to add a new cell.

6. **Execute the cell.** Call
   `mcp__jupyter-compute__execute_cell(cell_index=N)` with a reasonable
   timeout. Paste the actual stdout / errors verbatim — don't paraphrase.

7. **Iterate or wrap up.** If the cell errored, propose the next fix and
   loop back to step 4. If it succeeded, ask the user whether to continue
   or stop.

> **Do NOT use** `notebook_run-all-cells` — it is exposed by the bridge
> but currently returns 404 (asymmetric upstream bug, see
> `docs/setup/jupyter-mcp.md` "Investigation log" §5). Loop
> `execute_cell(cell_index=i)` over indices instead when a multi-cell run
> is needed.
