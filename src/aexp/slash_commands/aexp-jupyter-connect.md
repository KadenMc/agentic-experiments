---
description: "Connect (or switch) to a specific Jupyter the user has running, using a port number or SLURM job hint."
---

Help the user connect to a specific Jupyter server, either fresh or as a
mid-session switch. Argument: `<port-or-job-hint>` — e.g. `3618`,
`8562`, the SLURM job id (`12345`), or part of the SLURM job name
(`train`).

> **Prerequisite.** A `mcp__jupyter*__connect_to_jupyter` tool must be
> available (installed via `aexp install --with-jupyter`).

Run through these steps:

1. **Verify the connect tool exists.** If `connect_to_jupyter` is not in
   your tool list, stop and report: "Jupyter MCP not installed —
   run `aexp install --with-jupyter` and restart Claude Code."

2. **Resolve the bootstrap path.**
   - **No existing connection.** Ask the user for the bootstrap URL +
     token (cannot discover sight-unseen). Once they provide it, call
     `connect_to_jupyter(jupyter_url=...)`. The PostToolUse hook will
     surface the re-introspection directive; follow it.
   - **Existing connection.** Skip to step 3 — discover siblings first
     so the switch can be name-based.

3. **Discover what's available.** Run:
   ```
   execute_code(code="from aexp.jupyter import init; import json; print(json.dumps(init().model_dump(), default=str))")
   ```
   Pass the output to `mcp__aexp__jupyter_parse_introspection`. The
   `cluster_siblings` field lists every other Jupyter visible from the
   current kernel (cluster-wide on shared-home HPC).

4. **Match the user's hint.** Try, in order:
   - exact port match against `sibling.port`;
   - exact SLURM job id match (cross-reference: for each sibling, do you
     have its SLURM job id? If not, you may need to dispatch the
     introspection recipe to the sibling via a temporary
     `connect_to_jupyter` — but that means *switching first*, which
     defeats the purpose. In practice, fall back to port + hostname);
   - substring match against `sibling.url`.

   If multiple candidates match, list them and ask the user to pick.
   If none match, list all siblings and ask.

5. **Execute the switch.** Call
   `connect_to_jupyter(jupyter_url=<resolved-url>)`.

6. **Honor the PostToolUse directive.** The `jupyter_connect_postuse`
   hook will print a re-init directive into your context. Immediately
   dispatch the recipe it names (`from aexp.jupyter import init; ...`)
   on the *newly connected* Jupyter, parse via
   `mcp__aexp__jupyter_parse_introspection`, and show the user the
   resulting SessionInfo so they can confirm the switch landed where
   intended.

7. **Stop on mismatch.** If the SessionInfo's host / SLURM job / port
   does not match what the user asked for, do NOT proceed with any
   `execute_code` / `execute_cell` calls. Report the discrepancy.

> **Why the redundant re-introspection?** No MCP tool exposes "which URL
> am I currently connected to." Without re-introspection after every
> switch, an agent can be wrong about its connection state and execute
> code in the wrong kernel — at worst, in someone's training run.
