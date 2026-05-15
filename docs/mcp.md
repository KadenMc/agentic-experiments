# MCP server

`aexp` ships an MCP (Model Context Protocol) server so Claude Code picks
up its operations as **first-class typed tool calls** — not shell commands.
Each CLI verb has a corresponding MCP tool that returns structured JSON
instead of a `rich.Table`, so the agent can branch on the data without
parsing CLI output.

## When to use MCP tools vs slash commands vs CLI

| Surface | Triggered by | Best for |
|---|---|---|
| **MCP tool** (`new_run`, `list_runs`, ...) | Claude during a turn | Structured queries, tool chaining, programmatic flows |
| **Slash command** (`/aexp-new-run`, ...) | User typing `/aexp-…` | Multi-step guided workflows |
| **CLI** (`aexp new-run ...`) | Human at a terminal | Scripts, CI, PowerShell sessions |

All three invoke the same canonical Python API under the hood.

## Install (teammate perspective)

One one-time prerequisite per machine: install [`uv`](https://docs.astral.sh/uv/).

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone a repo that has `aexp install` already run (so `.mcp.json` is
in git) and open Claude Code. Done. `uvx` fetches
`agentic-experiments` from PyPI on first use and runs the MCP server in
an isolated environment.

## How the invocation works

`aexp install` writes this to `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "aexp": {
      "command": "uvx",
      "args": [
        "--from",
        "agentic-experiments[mcp]",
        "aexp-mcp-server"
      ],
      "env": {"PYTHONUNBUFFERED": "1"}
    }
  }
}
```

What's happening:

- `uvx` is on PATH for anyone with `uv` installed. Claude Code spawns
  it directly as a concrete Windows/macOS/Linux binary.
- `--from agentic-experiments[mcp]` tells uvx to fetch the
  `agentic-experiments` distribution from PyPI with the `[mcp]` extra
  (the `mcp` SDK dep). It caches under `~/.cache/uv`, so subsequent
  sessions reuse the same venv.
- `aexp-mcp-server` is the entry-point script the package exposes
  (declared in `pyproject.toml`'s `[project.scripts]`). It runs
  `aexp.mcp_server:main()` which calls `mcp.run()` over stdio.
- `PYTHONUNBUFFERED=1` ensures stdio isn't buffered, preventing JSON-RPC
  framing delays.

This pattern is canonical: every Python reference server under
[modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers)
uses `uvx`, and it's the pattern Anthropic's MCP Python quickstart
documents.

## `.mcp.json` is committable to git

Unlike env-specific invocations (absolute paths, conda runs), the uvx
form above is **identical on every machine**. Commit `.mcp.json` and all
teammates get the MCP server on clone.

> **Important:** Claude Code does NOT read `mcpServers` from
> `.claude/settings.json` — that file is for hooks + permissions only.
> MCP servers live in `.mcp.json` (project scope) or `~/.claude.json`
> (local / user scope). `aexp install` picks the project-scope path so
> the configuration travels with the repo.

## Available tools

| Tool | Purpose | Required args |
|---|---|---|
| `new_run` | Create a signac job linked to a Limina experiment | `experiment_id` |
| `list_runs` | Filter runs by experiment / hypothesis / status | — |
| `list_batches` | Group runs into `(experiment_id, condition)` slices | — |
| `show_run` | Full state point + doc + workspace for one run | `job_id` |
| `show_batch` | Runs matching a batch selector | `experiment_id` |
| `link_run` | Retroactively stamp `doc["limina"]` onto a job | `job_id`, `experiment_id` |
| `bind_tracker` | Attach a noop or wandb tracker to a run | `job_id` |
| `validate` | Compose KB + run-link + finding-citation checks | — |
| `sync_offline` | `wandb sync` every offline run in the store | — |
| `queue_stop` | Interrupt a running queued job; transitions to `"stopped"` | `job_id` |
| `jupyter_introspect_current` | Returns the recipe for live-introspecting the connected kernel via the Jupyter MCP's `execute_code`. Pair with `jupyter_parse_introspection`. | — |
| `jupyter_parse_introspection` | Parses the stdout of an `aexp.jupyter.init()` dispatch into a structured `SessionInfo`. | `raw_output` |

All return JSON-serializable dicts. Errors surface either as
`{"error": ..., "code": ...}` in the return value or as MCP error
responses (for tool exceptions). The server never crashes.

## Verification prompt

After `aexp install` + restart Claude Code:

```markdown
I want to verify the aexp MCP server is connected and its tools work.

1. List the MCP tools you have available in this session. Report back
   every tool name whose server identifies as "aexp".
2. Call the `validate` tool (no arguments). Report the `ok` field and
   how many `errors` / `warnings` came back.
3. Call the `list_runs` tool (no arguments). Report the length of the
   returned list.
4. Call the `new_run` tool with `experiment_id="E001"`,
   `statepoint={"condition": "smoke"}`. Report the `job_id` it returns.
5. Call the `validate` tool again with `mode="runs-only"`. Confirm that
   the new run shows up as `run.broken_experiment_link` (expected — the
   kb/ is empty, so E001 has no backing file).

Do NOT use the CLI or Bash tool for any of the above. Use MCP tool calls
only. If you can't see any "aexp" tools, say so immediately.
```

## Troubleshooting

**`uvx: command not found`** — install `uv` (one of the one-liners above).

**Server shows `✓ Connected` in `/mcp` but no aexp tools in-session** —
this was the Windows Claude Code stdio bug we migrated off. Confirm
`.mcp.json` uses the `uvx` invocation (not `conda run`, not absolute
Python path). If it's stale from an older install, re-run
`aexp install --force` to regenerate.

**Developing `aexp` itself and want editable-install edits to reach the MCP server?**
Pass `--dev` to `aexp install`:

```bash
aexp install --dev --yes
```

That writes a `.mcp.json` whose `aexp` entry invokes the current Python
interpreter directly instead of going through `uvx`:

```json
{
  "mcpServers": {
    "aexp": {
      "command": "<absolute-path-to-your-env-python>",
      "args": ["-m", "aexp.mcp_server"],
      "env": {"PYTHONUNBUFFERED": "1"}
    }
  }
}
```

Because this form bakes in a machine-specific path, **do not commit the
dev-form `.mcp.json`**. Gitignore it while iterating, or re-run
`aexp install --force` (without `--dev`) to regenerate the portable
uvx form before committing.

MCP-layer edits don't hot-reload — after changing `aexp.mcp_server` or
any module it imports, restart Claude Code (or `/mcp` -> disconnect/
reconnect the `aexp` server) to pick up the change. Hook and CLI
changes are picked up on next invocation automatically.

**Smoke the server command directly:**

```powershell
uvx --from agentic-experiments[mcp] aexp-mcp-server
# Expected: the process starts and hangs reading stdin (MCP stdio
# transport). Ctrl+C to quit. If it errors, fix before retrying.
```

### Fallback 1: user-scope install (skips approval prompt)

Project-scope servers prompt for trust on first use. If that fails, register
at user scope to bypass:

```powershell
claude mcp add --scope user aexp -- uvx --from agentic-experiments[mcp] aexp-mcp-server
```

### Fallback 2: HTTP transport (bypasses any stdio issues)

If you still hit transport problems (unlikely with uvx but possible),
switch to HTTP. Run the server as a standalone process:

```powershell
python -c "from aexp.mcp_server import mcp; mcp.run(transport='streamable-http', port=8765)"
```

Edit `.mcp.json`:

```json
{"mcpServers": {"aexp": {"type": "http", "url": "http://localhost:8765/mcp"}}}
```

## How this relates to the canonical Python API

Every MCP tool is a one-line wrapper around a function in `aexp.*`. The
canonical Python API is what's tested most thoroughly; the MCP layer is
a serialization adapter. If you're scripting, prefer the Python API
directly:

```python
from aexp import create_run, validate_repo

job = create_run(experiment_id="E018", statepoint={"condition": "full"})
result = validate_repo()
```

MCP is for the agent; Python is for code; CLI is for humans.
