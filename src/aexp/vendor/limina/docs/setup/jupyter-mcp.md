# Claude Code ↔ JupyterLab on a remote cluster (via MCP)

Lets a local Claude Code or Claude Desktop agent read, edit, and execute
notebook cells against a JupyterLab process running on a remote compute
node, **through an existing SSH-tunneled port** — no agent SSH, no
internet on the compute node, no extra cluster permissions.

Reference: [Datalayer jupyter-mcp-server](https://github.com/datalayer/jupyter-mcp-server).
Their docs assume a clean pip install on a single machine; this guide is
the cluster-specific extension.

> **Local overlay note.** This file is the canonical, vendor-managed
> version that ships with `agentic-experiments`. It will be overwritten
> on `aexp install --force` to pick up upstream fixes. If you need
> project-specific overlay info (your cluster hostname, your conda env
> name, lab-specific paths), put it in a sibling file like
> `docs/setup/jupyter-mcp-local.md` so re-installs don't clobber it.

## Adapting this guide to your cluster

Fill in your values once and refer back to them in the rest of the doc:

| Placeholder | Your value | Notes |
|---|---|---|
| `<your-username>` | | login on the cluster |
| `<your-cluster-login-host>` | | e.g. `cluster.example.org` |
| `<your-compute-node-fqdn>` | | printed by your sbatch script |
| `<repo-root-on-cluster>` | | e.g. `/cluster/home/<user>/<repo>` |
| `<your-conda-env>` | | env hosting JupyterLab + jupyter-mcp-server |
| `<your-token>` | | the stable token you generate (see "Stable token setup") |
| `<port>` | `3618` | default; change only if it collides |

## TL;DR

One MCP server runs on the laptop, reaching the cluster JupyterLab
through your SSH-tunneled port:

- **`jupyter`** — laptop-side `uvx jupyter-mcp-server` (stdio transport,
  MCP_SERVER mode). The target Jupyter URL + token are supplied
  per-session at runtime via the `connect_to_jupyter` tool — nothing is
  baked into `.mcp.json`, so the *same* entry retargets to any node:
  open a tunnel on a free local port, call `connect_to_jupyter` at the
  new URL, done — no config edit, no MCP restart. That runtime
  retargeting is what makes the multi-node workflow
  (`/aexp-jupyter-connect`, `/aexp-jupyter-discover`) work.

It exposes ~17 core tools (read/edit/execute cells, kernel ops, etc.).

## ⚠️ The Datalayer extension disable list

When you install `jupyter-collaboration` and `jupyter-mcp-server`,
several **experimental Datalayer extensions get pulled in transitively
and auto-enable on install**. They conflict with the mainstream Project
Jupyter stack `jupyter-mcp-server` actually relies on. Each must be
disabled explicitly. `aexp jupyter-setup` automates this — see "One-time
cluster setup" below.

**Server extensions to DISABLE:**

| Extension | Symptom if left enabled | Disable command |
|---|---|---|
| `jupyter_server_documents` | Replaces kernel manager with `NextGenKernelManager`; cell-document ops 404 with `SchemaRegistryException` for duplicate schema; `execute_code` hangs to timeout | `jupyter server extension disable jupyter_server_documents` |

**Server extensions to KEEP ENABLED** (some are auto-enabled, but be
aware):

| Extension | Why keep it | Note |
|---|---|---|
| `jupyter_server_nbmodel` | Required for `execute_cell` and document-mutation tools (`insert_cell`, `delete_cell`, etc.) in JUPYTER_SERVER mode | Initial debugging blamed nbmodel for the File ID error, but the real culprit was `@jupyter-ai-contrib/server-documents` (frontend). Keep nbmodel enabled. |
| `jupyter_server_ydoc` | Provides `/api/collaboration/session/...` routes for cell-document ops | Some envs ship it disabled; explicitly enable: `jupyter server extension enable jupyter_server_ydoc` |

**Frontend (lab) extensions to DISABLE:**

| Extension | Symptom if left enabled | Disable command |
|---|---|---|
| `@jupyter-ai-contrib/server-documents` | Frontend JS keeps calling Datalayer-private routes (`/api/fileid/index`, `/jupyter-server-documents/get-example`) that don't exist after disabling the server-side `jupyter_server_documents`. Notebooks fail to open with "File ID error: ... cannot be opened because its file ID could not be retrieved." | `jupyter labextension disable @jupyter-ai-contrib/server-documents` |

> **Why two layers.** Datalayer ships paired *server* extensions and
> *frontend* labextensions. Disabling one without the other leaves the
> frontend bundled to call routes that no longer exist. When you disable
> `jupyter_server_documents`, you must also disable
> `@jupyter-ai-contrib/server-documents` so the frontend stops calling
> its private endpoints and falls back to the standard
> `@jupyter/collaboration-extension` paths that `jupyter-server-ydoc`
> actually serves.

If a future install pulls in a *new* `jupyter_server_*` (or
`@jupyter-ai-contrib/*` / `@datalayer/*`) extension you didn't
explicitly install — disable it first, see if things work, only
re-enable if needed.

## Architecture

```
laptop (has internet)
    │
    └─ Claude  ──stdio──→  uvx jupyter-mcp-server  (MCP_SERVER mode, "jupyter")
                                 │
                                 │  HTTP+WS via tunnel
                                 ▼
          ssh -N -L <port>:<compute-node>:<port> ── login node ── compute node (no internet)
                                                                       │
                                                                       │  jupyter lab
                                                                       ▼
                                                               <repo-root-on-cluster>
                                                               (--ServerApp.root_dir confines content manager)
```

The MCP server runs on the laptop and reaches the remote Jupyter over
standard HTTP + kernel-WebSocket through the SSH tunnel. Only the laptop
has internet; the compute node never reaches outside.

## One-time cluster setup

```bash
# On the login node
ssh <your-username>@<your-cluster-login-host>
conda activate <your-conda-env>
cd <repo-root-on-cluster>

# Install the agentic-experiments [jupyter] extra (brings in
# jupyter-collaboration, jupyter-mcp-server, jupyter-mcp-tools at
# verified versions). With Poetry:
poetry add --group=jupyter-mcp \
  'jupyter-collaboration==4.0.2' \
  jupyter-mcp-tools \
  jupyter-mcp-server

# Pin jupyterlab to the tested version (prevents kernel-WS regressions)
poetry add --group=dev "jupyterlab==4.4.1"

# Apply the verified extension state in one step:
aexp jupyter-setup

# Or do it by hand:
jupyter server extension disable jupyter_server_documents
jupyter labextension disable @jupyter-ai-contrib/server-documents
jupyter server extension enable jupyter_server_ydoc
jupyter server extension enable jupyter_server_nbmodel

# Verify the final state
jupyter server extension list 2>&1 \
  | grep -iE "ydoc|documents|fileid|nbmodel|mcp"
```

Expected (the canonical working configuration):

| Extension | State |
|---|---|
| `jupyter_server_ydoc` | enabled |
| `jupyter_server_fileid` | enabled |
| `jupyter_server_nbmodel` | **enabled** (required for `execute_cell` in JUPYTER_SERVER mode) |
| `jupyter_mcp_server` | enabled |
| `jupyter_mcp_tools` | enabled |
| `jupyter_server_documents` | **disabled** |
| `@jupyter-ai-contrib/server-documents` (labextension) | **disabled** |

> **Version pins.** Datalayer's `jupyter-mcp-server` README documents
> `jupyterlab==4.4.1 jupyter-collaboration==4.0.2` as the tested
> combination, and that is what the `[jupyter]` extra resolves to.
> Newer versions may also work but are not re-verified — leave the pins
> unless something else in the project requires `>=4.5.0`.

## One-time laptop setup

You need two things on the laptop:

1. **`uv`** (for `uvx`) — runs the laptop-side MCP server
2. **`.mcp.json` entry** — written by `aexp install --with-jupyter`

```powershell
# Install uv if not already there
pip install uv

# Verify uvx can fetch jupyter-mcp-server (does NOT support --help cleanly,
# but a non-error exit means it's installed)
uvx jupyter-mcp-server --help   # may exit with usage; that's fine
```

After `aexp install --with-jupyter` your `.mcp.json` will include the
`jupyter` server:

```json
{
  "mcpServers": {
    "jupyter": {
      "command": "uvx",
      "args": ["jupyter-mcp-server"]
    }
  }
}
```

No token or URL is baked into the entry — the agent supplies them at
runtime via `connect_to_jupyter` (see "Per-session: connect from the
laptop" below).

## Per-session: launch JupyterLab on the cluster

Use whatever batch launcher you have for JupyterLab. The launcher should
print a `===== LOCAL CONNECTION =====` block with three lines:

1. **Tunnel** — `ssh -N -L <port>:<compute-node>:<port> <your-username>@<your-cluster-login-host>`
2. **Browser** — `http://127.0.0.1:<port>/lab?token=<your-token>`
3. **Claude prompt** — `Connect to jupyter at http://127.0.0.1:<port> with token <your-token>. List the notebooks.`

If you're using a stable token (recommended — see below), only the host
of line 1 changes per job; lines 2 and 3 are the same every time.

## Per-session: connect from the laptop

```powershell
# Open the SSH tunnel
ssh -N -L <port>:<your-compute-node-fqdn>:<port> <your-username>@<your-cluster-login-host>

# (Optional) sanity check — paste line 2 of the launcher log into Chrome.
# JupyterLab should open with <repo-root-on-cluster> as the file root.
```

Open a fresh Claude session; the `mcp__jupyter__*` tool family should
appear. Then point it at the running Jupyter — paste line 3 of the
launcher log, or just tell the agent:

```
Connect to jupyter at http://127.0.0.1:<port> with token <your-token>.
```

The agent calls `connect_to_jupyter(jupyter_url, jupyter_token)`; a
PostToolUse hook then surfaces a re-introspection directive it follows
to confirm the session landed where intended. To switch to a different
Jupyter mid-session, use `/aexp-jupyter-connect`.

### Stable token setup (one-time, recommended)

The MCP server takes the token at runtime via `connect_to_jupyter`, so
there is no token in `.mcp.json` to keep in sync. But a *stable* token
still helps: it means the connect prompt (line 3 of the launcher log) is
identical for every job, so you paste the same string each time instead
of copying a fresh random token.

**On the cluster** (one-time):

```bash
# Generate a token and write to user-level Jupyter config — applies to
# ALL jupyter lab launches
mkdir -p ~/.jupyter
TOKEN=$(openssl rand -hex 32)
echo "c.IdentityProvider.token = '$TOKEN'" > ~/.jupyter/jupyter_server_config.py
chmod 600 ~/.jupyter/jupyter_server_config.py
echo "JUPYTER_TOKEN=$TOKEN"  # this is the token you paste into the connect prompt
```

Then update your batch launcher script:
- **Remove** any `JUPYTER_TOKEN="${JUPYTER_TOKEN:-$(openssl rand -hex 32)}"` line (no longer needed)
- **Remove** `--IdentityProvider.token="$JUPYTER_TOKEN"` from the `jupyter lab` invocation (the config file provides it)

After this, every launcher job uses the same token, so the connect
prompt never changes — only the tunnel's compute-node host varies per
job. The token is only valid against your SSH-tunneled localhost
endpoint (exploiting it requires your SSH key), so a private repo can
keep the connect prompt in its launcher log.

## Smoke test

Paste this into a fresh agent (substitute the token):

```
Connect to my Jupyter at http://127.0.0.1:<port> with token <your-token>.

1. `list_kernels` — should be empty or only one fresh entry.
2. `use_notebook` to open `notebooks/<some_smoke_notebook>.ipynb`.
3. `execute_code` a CPU sanity check (`import sys; print(sys.executable); print("alive")`). Must return in <2s.
4. `execute_code` torch+CUDA: `import torch; print(torch.__version__, torch.cuda.is_available())`. Should complete in <10s.
5. `insert_cell` + `execute_cell` + `delete_cell` — full cycle.
```

If all five pass, the integration is verified end-to-end.

## Live session introspection

Once the agent has connected to *any* Jupyter the user is running,
`aexp.jupyter.init()` recovers everything about that session from live
state — no registry, no marker files.

### What gets recovered

| Field | Source |
|---|---|
| `jupyter_url` / `jupyter_port` / `jupyter_token` / `jupyter_root_dir` / `jupyter_pid` | `jupyter_server.serverapp.list_running_servers()` filtered by `JPY_PARENT_PID` |
| `kernel_id` | `IPKernelApp.connection_file` |
| `cgroup` | `/proc/self/cgroup` (Linux only) |
| `slurm.job_id` | `job_<id>` segment in `/proc/self/cgroup`, falling back to `$SLURM_JOB_ID` |
| `slurm.job_name` / `state` / `runtime` / `time_limit` / `nodelist` / `partition` / `user` | `squeue -h -j <id> -o ...` |
| `slurm.submit_time` / `start_time` | `scontrol show job <id>` |
| `attached_notebooks` | Jupyter HTTP `/api/sessions` against the current server |
| `gpu_processes` | `nvidia-smi --query-compute-apps=...` |
| `cluster_siblings` | `list_running_servers()` minus the current PID; on shared-home HPC this enumerates cluster-wide |

Every probe degrades gracefully when its prerequisite is missing:
laptop with no SLURM → `slurm = None`; no GPU →
`gpu_processes = []`; non-Linux → `cgroup = None`.

### Calling it

From inside any kernel (the agent dispatches this via the Jupyter MCP's
`execute_code`):

```python
from aexp.jupyter import init
import json
print(json.dumps(init().model_dump(), default=str))
```

Or from a Jupyter terminal / shell:

```bash
aexp jupyter init --json     # canonical SessionInfo dump
aexp jupyter whoami          # human-readable summary
aexp jupyter discover        # list other Jupyters the user is running
aexp jupyter discover --describe   # ...with attached notebooks + kernel state
```

### Multi-Jupyter workflow

If you're running multiple Jupyters (e.g. two SLURM jobs on the same
cluster), one is enough to bootstrap the agent. From there:

1. `/aexp-jupyter-discover` lists every other Jupyter visible from the
   current kernel.
2. `/aexp-jupyter-connect <port-or-hint>` switches the active
   connection. A PostToolUse hook (`jupyter_connect_postuse`) fires
   automatically after every `connect_to_jupyter` call, surfacing a
   directive that the agent must immediately re-run `init()` and
   verify the SessionInfo before executing any code.

### Stating policy intent

There is no persistent "do-not-touch" registry in v1. If you want the
agent to leave a specific kernel alone for the rest of a conversation
(e.g. you have something fragile running that introspection won't
catch), simply tell it so in chat: *"don't touch the kernel attached to
notebook X for the rest of this session."* The agent honors that for
the conversation; nothing is written to disk.

If you find yourself needing cross-session enforcement, that's an
additive follow-up — open an issue.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| **"File ID error: ... cannot be opened because its file ID could not be retrieved"** when opening any notebook in JupyterLab UI. Server log shows `404 POST /api/fileid/index` and `404 GET /jupyter-server-documents/get-example` | Frontend labextension `@jupyter-ai-contrib/server-documents` is calling Datalayer-private routes that no longer exist after we disabled the server-side `jupyter_server_documents` | `jupyter labextension disable @jupyter-ai-contrib/server-documents`, then restart the lab process |
| `execute_cell` returns `jupyter_server_nbmodel extension not found. Please install it.` | `jupyter_server_nbmodel` is disabled (you may have over-corrected if you were following an older draft of this doc) | `jupyter server extension enable jupyter_server_nbmodel`, then restart the lab process |
| `404 Not Found for url: http://.../api/collaboration/session/...` | `jupyter_server_ydoc` extension disabled | `jupyter server extension enable jupyter_server_ydoc`, then restart the lab process |
| `read_cell` works but every `execute_code` / `execute_cell` hangs to timeout, even on trivial `print("alive")` | `jupyter_server_documents` extension is loaded — replaces kernel manager and breaks WebSocket path | `jupyter server extension disable jupyter_server_documents`, then restart the lab process |
| Kernel hangs on first `import torch` for 60s+ then channel goes dead | Cold CUDA driver init blocks ipykernel main thread without releasing GIL | Pre-warm at job start: add `python -c "import torch; _ = torch.zeros(1).cuda()"` to your batch script before launching jupyter, OR set `PYTORCH_NVML_BASED_CUDA_CHECK=1`. To recover a wedged kernel without canceling the job: `curl -X DELETE http://localhost:<port>/api/kernels/<kid>?token=<token>`, then re-create. |
| `use_notebook` ignores notebook's kernelspec metadata and creates a `python3` kernel | Known minor MCP-server quirk; underlying interpreter is still the right env, but the kernelspec label is wrong | Pre-create the kernel via `curl -X POST .../api/kernels -d '{"name":"<env>"}'` then pass the returned id to `use_notebook(kernel_id=...)` |
| Manual notebook edits in browser tab stop persisting after Claude has edited cells | [datalayer/jupyter-mcp-server#146](https://github.com/datalayer/jupyter-mcp-server/issues/146) — Y.js sync corruption | Stop using MCP on that notebook; reload the tab. If this becomes recurrent, consider building a narrower custom Jupyter Server extension exposing only the four tools you actually need. |
| `poetry add` hangs / silently bails / `Resolving dependencies... (0.0s)` | Broken `file://` path dep in pyproject.toml getting parsed before markers are evaluated ([poetry#9679](https://github.com/python-poetry/poetry/issues/9679)) | Don't add `file://` path deps with `sys_platform` markers. Move sibling editable installs to `poetry run pip install -e ...` outside pyproject.toml |
| Claude tries to use `list_notebooks` before opening any | The tool only lists *opened* notebooks, not files | Tell it to use `list_files` to enumerate `.ipynb` files |

## Investigation log — how we arrived at this configuration

The verified-working state above wasn't obvious. It took a multi-day
session of progressive narrowing to find it. This section preserves the
chronology so symptoms are recognizable next time and so the *why* of
each decision survives.

### Round 1: kernel WebSocket silently broken (`jupyter_server_documents` is the culprit)

**Symptom:** `read_cell` / `insert_cell` / `delete_cell` worked, but
every `execute_code` and `execute_cell` hung to the MCP timeout. Even a
trivial `print("alive")` on a fresh kernel never returned. Kernel showed
`state=idle` but `last_activity` frozen and `connections=0`. After
timeout, follow-up calls returned `Connection is already closed`.

**Diagnostic that nailed it:** Direct WebSocket handshake from the
JupyterLab terminal:
```bash
curl -v -H "Upgrade: websocket" -H "Connection: Upgrade" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
  "http://localhost:<port>/api/kernels/$KID/channels?token=$TOKEN&session_id=test"
```
Returned `101 Switching Protocols` and streamed real `kernel_info_reply`
messages. **The kernel WebSocket was healthy.** The bug was in the MCP
server's interaction with the kernel manager.

**Root cause:** `jupyter_server_documents` (Datalayer-experimental,
ships transitively with `jupyter-collaboration`, auto-enables on
install) installs `NextGenKernelManager` — not a subclass of
`ServerKernelManager`. The MCP server's `jupyter-kernel-client` expects
the standard manager and silently fails when the substituted one is in
use. Server log shows:
```
KernelManager class 'NextGenKernelManager' is not a subclass of 'ServerKernelManager'.
jupyter_server_ydoc | extension failed loading with message: SchemaRegistryException(
  'The schema, https://schema.jupyter.org/jupyter_collaboration/session/v1, is already registered.'
)
```

**Fix:** `jupyter server extension disable jupyter_server_documents`.

### Round 2: File ID error on notebook open (`@jupyter-ai-contrib/server-documents` frontend labextension)

**Symptom (after Round 1 fix):** Opening any notebook in JupyterLab UI
returned:
> File ID error
> The file 'notebooks/...' cannot be opened because its file ID could
> not be retrieved.

Server log showed:
```
404 GET /jupyter-server-documents/get-example
404 POST /api/fileid/index?path=notebooks/...
```

**Initial wrong hypothesis:** First suspect was that
`jupyter_server_nbmodel` (also auto-enabled with `jupyter-mcp-server`
install) was breaking `jupyter_server_ydoc` initialization. Disabled
it. **Error persisted.**

**Real cause:** The JupyterLab *frontend* labextension
`@jupyter-ai-contrib/server-documents` was still loaded in the browser
tab and was bundled to call Datalayer-private routes (`/api/fileid/index`,
`/jupyter-server-documents/get-example`) that disappeared when
`jupyter_server_documents` was disabled in Round 1. `/api/fileid/index`
is **not** a standard `jupyter-server-fileid` route (which only
registers `/api/fileid/id` and `/api/fileid/path`).

**Fix:** `jupyter labextension disable @jupyter-ai-contrib/server-documents`.
Notebooks open cleanly. **Pattern: Datalayer ships paired server+frontend
extensions; disabling one half leaves the other half calling dead
routes.**

### Round 3: `execute_cell` requires `jupyter_server_nbmodel` after all

**Symptom:** With Rounds 1+2 fixed and `jupyter_server_nbmodel` still
disabled (from the wrong-hypothesis attempt), `execute_cell` returned:
> jupyter_server_nbmodel extension not found. Please install it.

`execute_code` (direct kernel) worked. `read_cell` worked. But anything
that needed to write back to the notebook document model (execute +
persist outputs, insert, delete, etc.) failed.

**Root cause:** In JUPYTER_SERVER mode, `jupyter-mcp-server`'s
`execute_cell` delegates to `jupyter_server_nbmodel`'s document
execution API. (In MCP_SERVER mode it uses `jupyter-kernel-client` over
WebSocket directly, no nbmodel needed — which is why earlier MCP_SERVER
testing didn't hit this.)

**Fix:** `jupyter server extension enable jupyter_server_nbmodel`.
Re-tested notebook open — **File ID error did NOT return.** Confirmed
nbmodel was always innocent of Round 2's symptom; the frontend
labextension was the only real culprit.

**Lesson:** when chasing a symptom across multiple Datalayer extensions,
**disable one at a time and re-test**, instead of disabling everything
that *might* be at fault.

### What this means for first-time setup

If you're setting this up fresh on a new env and following the recipe at
the top of this document (or running `aexp jupyter-setup`), you should
never see Rounds 1-3 — the recipe disables/enables the right things
from the start. But if you ever:

- Hit the kernel-WS-hangs-silently symptom → Round 1
  (`jupyter_server_documents`)
- Hit the File ID error → Round 2 (frontend labextension
  `@jupyter-ai-contrib/server-documents`), NOT `nbmodel`
- Hit `execute_cell returns "nbmodel not found"` → Round 3 (re-enable
  `jupyter_server_nbmodel`)

The matching troubleshooting-table entries above link to these rounds.

## Why the off-the-shelf integration vs a custom extension?

This guide uses the off-the-shelf `jupyter-mcp-server`. It works, but:
(a) the tool surface is broad (`list_files` exposes everything Jupyter
can see — confined here by `--ServerApp.root_dir`, but `execute_code`
is unconstrained Python in the kernel), and (b) issue #146 (Y.js sync
corruption when Claude and a human edit the same notebook) is unfixed
upstream.

A natural follow-up if friction becomes real or PHI-scoping needs to be
defensible to compliance: build a small custom Jupyter Server extension
exposing exactly four narrow MCP tools (`read_cell`, `set_cell_source`,
`execute_cell`, `read_cell_output`) with audit log and consent gate.
Not strictly necessary for most use cases.

## Environment reference (verified working)

This is the exact stack the integration was verified against. If
something breaks later and you suspect drift, compare against this
snapshot.

### Cluster

**Core Jupyter:**

| Package | Version | Notes |
|---|---|---|
| `python` | 3.12.x | from conda-forge |
| `jupyterlab` | 4.4.1 | pinned in `pyproject.toml` `[dependency-groups] dev` |
| `jupyter-server` | 2.17.0 | |
| `jupyter-collaboration` | 4.0.2 | pinned in `[dependency-groups] jupyter-mcp` |
| `jupyter-server-ydoc` | 2.3.0 | enabled |
| `jupyter-server-fileid` | 0.9.3 | enabled (uses `ArbitraryFileIdManager`) |
| `jupyter-server-nbmodel` | 0.1.1a4 | enabled — required for `execute_cell` in JUPYTER_SERVER mode |
| `jupyter-collaboration-ui` | 2.3.0 | frontend; transitive |
| `jupyter-docprovider` | 2.3.0 | transitive |
| `jupyter-ydoc` | 3.4.1 | transitive |
| `pycrdt` | 0.12.50 | transitive (Y.js bindings) |
| `pycrdt-websocket` | 0.16.0 | transitive |
| `pycrdt-store` | 0.1.3 | transitive |

**Datalayer MCP stack:**

| Package | Version | Notes |
|---|---|---|
| `jupyter-mcp-server` | 1.0.2 | enabled — registers `/mcp` SSE endpoint |
| `jupyter-mcp-tools` | 0.1.6 | enabled — frontend extension for UI bridge |
| `jupyter-kernel-client` | 0.9.0 | transitive |
| `jupyter-nbmodel-client` | 0.14.7 | transitive |
| `jupyter-server-client` | 0.1.1 | transitive |

**Disabled (intentionally):**

| Package | Version | Why |
|---|---|---|
| `jupyter-server-documents` | 0.1.1 | server extension — replaces kernel manager, breaks MCP kernel WS path |
| `@jupyter-ai-contrib/server-documents` | 0.1.1 | frontend labextension — calls Datalayer-private routes that don't exist after server extension is disabled |

### Laptop

| Tool | Version | Notes |
|---|---|---|
| `uv` | latest | Used by `uvx jupyter-mcp-server` for the laptop-side MCP server |
| `jupyter-mcp-server` | 1.0.2+ | fetched ephemerally by `uvx`; not permanently installed |

### Cluster server endpoint (when Jupyter is running)

| Endpoint | Purpose |
|---|---|
| `http://127.0.0.1:<port>/lab?token=<token>` | JupyterLab UI (open in browser) |
| `http://127.0.0.1:<port>/api/kernels` | Kernel REST API (list, create, delete) |
| `ws://127.0.0.1:<port>/api/kernels/<id>/channels` | Kernel WebSocket (used by MCP_SERVER mode kernel-client) |
| `http://127.0.0.1:<port>/api/collaboration/session/<path>` | Y.js doc room session (PUT to register; required for cell-document ops) |
| `http://127.0.0.1:<port>/api/fileid/id` and `.../path` | Standard file-ID lookup (jupyter-server-fileid) |

### Re-capture this snapshot later

When debugging future drift, run this on the cluster to dump the current
state for diff against the table above:

```bash
# On the login node, in the right env:
conda activate <your-conda-env>
cd <repo-root-on-cluster>

echo "=== Python ==="
python --version

echo "=== Jupyter packages ==="
pip list 2>/dev/null | grep -iE "jupyter|pycrdt|datalayer|mcp" | sort

echo "=== Server extensions ==="
jupyter server extension list 2>&1 | grep -E "enabled|disabled"

echo "=== Lab extensions ==="
jupyter labextension list 2>&1 | grep -iE "enabled|disabled|jupyter|datalayer"
```

Save the output to a dated file (e.g.,
`docs/setup/jupyter-mcp-state-YYYY-MM-DD.txt`) when verifying a working
setup, so you have known-good snapshots to diff against.
