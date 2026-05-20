# Airgapped relay (`aexp.airgapped`)

> ## Do you need this?
>
> **Only if** the machine running your Jupyter kernel / agent has
> **no outbound internet**, but a sibling machine sharing `$HOME` *does*.
> This is tooling for the airgapped-compute case — most commonly seen on
> secure HPC clusters, but the pattern also shows up at regulated/clinical
> sites, government/research labs, and any setup where one machine is
> network-isolated by policy and a sibling reaches the internet.
>
> If `git pull` works from where you run Jupyter — your local machine,
> cloud VM, a cluster whose compute nodes have internet — **stop
> reading. You don't need this**, and importing `aexp.airgapped` would
> be dead weight. The
> module is not imported by `aexp` at package init for exactly this
> reason; nothing in your workflow changes if you ignore it.
>
> If you *are* airgapped on compute but have a sibling node with
> internet, read on.

`aexp.airgapped` is a thin SSH bridge that runs whitelisted git / wandb
commands on the internet-having sibling node on behalf of the airgapped
compute. The agent (and this module) live on your **local machine** (the
machine you run Claude Code from); each relay op is one `ssh` call to
the sibling node.

## Two topologies — which one are you?

### Topology A — Fully networked (most users, no relay needed)

```
   YOUR LOCAL MACHINE
   ──────────────────
   • Has internet
   • Runs Claude Code + aexp
   • Runs (or talks to) a Jupyter kernel that itself has internet
   • `git pull` / `git push` / `wandb sync` work directly

   You do not need `aexp.airgapped`. Don't import it. Stop here.
```

### Topology B — Airgapped compute (the case this module handles)

```
   YOUR LOCAL MACHINE              SIBLING NODE                  AIRGAPPED COMPUTE
   (where Claude Code runs)        (login node on an HPC,        (GPU node,
                                    jumpbox elsewhere)            locked-down workstation)
   ──────────────────              ─────────────────             ──────────────────
   • Has internet                  • Has internet                • No internet
   • Runs Claude Code + aexp       • Runs no service of yours    • Runs your Jupyter / GPU work
   • SSHes to the sibling node     • Just an SSH gateway         • Shares $HOME with sibling
                                   • Shares $HOME with compute     so the same git clone is
                                                                   visible to both
```

The compute side is where your actual work happens — but it can't reach
the internet, so `git pull` / `git push` / `wandb sync` won't work
there. The fix is to do those commands on the **sibling node** (which
has internet) against the **same `$HOME` clone** (which the airgapped
compute also sees). Your local machine reaches the sibling node by SSH;
the sibling node runs the git command; both sides see the result
because they share the filesystem.

## How it works

Each relay op is one `subprocess` call on your local machine:

```
ssh <host> "cd <remote_repo> && <whitelisted git/wandb command>"
```

```
   local machine (agent + aexp)                  sibling node (internet)
   ─────────────────────────────                 ────────────────────────
   RelayClient.pull()  ──►  ssh <host> "cd <repo> && git pull --ff-only"
                       ◄──  stdout + exit code
   returns RelayResult

                                  the sibling node shares $HOME with the
                                  airgapped compute, so the repo it
                                  operates on is the same clone the
                                  compute-side work sees.
```

There is no queue, no daemon, no heartbeat — each call is a short,
self-contained `ssh` round-trip. `ssh` is invoked with `BatchMode=yes`
so it never blocks on a prompt (a missing `known_hosts` entry or a
needed password fails fast instead of hanging an unattended agent), and
with `ConnectTimeout` so an unreachable host fails quickly.

## Whitelist

Only a fixed set of operations is allowed. The whitelist lives in
`aexp.airgapped.ALLOWED`:

| Op           | Command (login-node side)         | Consent  | Per-call args              |
| ------------ | --------------------------------- | -------- | -------------------------- |
| `git_pull`   | `git pull --ff-only`              | auto     | none                       |
| `git_push`   | `git push <args>`                 | auto     | `^[a-zA-Z0-9._/\-]+$` ×N   |
| `git_fetch`  | `git fetch --all --prune`         | auto     | none                       |
| `git_status` | `git status --porcelain=v2`       | auto     | none                       |
| `git_rebase` | `git pull --rebase`               | auto     | none                       |
| `wandb_sync` | `wandb sync --sync-all`           | **user** | none                       |

Auto-approved ops run immediately. The consent-required `wandb_sync`
requires the caller to pass `approve=True` (`--approve` on the CLI) — see
[Consent](#consent) below.

The whitelist is closed by design. There is no escape hatch for arbitrary
commands; every request passes through `aexp.airgapped.validate_request`,
which enforces:

- The op name is in `ALLOWED`.
- `args` is a list of strings; each arg matches the per-op regex if one is
  set; max 32 args, max 256 chars per arg.

Every token of the remote command is then `shlex.quote`-d for the remote
POSIX shell. The push-args regex already excludes shell metacharacters
(no spaces, `;`, `|`, `$`, backticks, quotes); quoting is defense-in-depth
and is what makes a `remote_repo` path containing spaces safe.

If you need a new op, extend `ALLOWED` and add the appropriate regex —
don't try to smuggle arbitrary commands through the existing entries.

## Configuration

Two values must be set — the SSH host and the remote repo path:

| Setting       | Constructor arg | Env var                  |
| ------------- | --------------- | ------------------------ |
| SSH host      | `ssh_host`      | `AEXP_RELAY_SSH_HOST`    |
| Remote repo   | `remote_repo`   | `AEXP_RELAY_REMOTE_REPO` |
| Audit log     | `audit_log`     | `AEXP_RELAY_AUDIT_LOG`   |

`ssh_host` should name a **`~/.ssh/config` Host alias**, not a bare
hostname — that way all of the auth detail (identity file, user, port,
MFA, connection multiplexing) lives in your SSH config, and this module
stays a thin wrapper. `remote_repo` is the absolute path of the git clone
on the login node (e.g. `~/electricrag`).

When both env vars are set, `RelayClient()` needs no arguments.

## Client API

The recommended entry point is `aexp.airgapped.RelayClient`:

```python
from aexp.airgapped import RelayClient

relay = RelayClient(ssh_host="cluster-login", remote_repo="~/electricrag")
r = relay.pull()
print(r.returncode, r.stdout)
```

Five git verbs are exposed as dedicated methods, plus a generic escape
hatch:

```python
relay.pull()                             # git pull --ff-only
relay.fetch()                            # git fetch --all --prune
relay.status()                           # git status --porcelain=v2
relay.rebase()                           # git pull --rebase
relay.push()                             # git push origin HEAD
relay.push(branch="feature/x")           # git push origin feature/x
relay.push(branch="x", remote="fork")    # git push fork x
relay.request("wandb_sync", approve=True, timeout=900)   # consent-required
```

Why dedicated methods for git? The raw `request("git_push")` call has two
arg-ordering frictions (documented as F7/F8):

- **F7**: `request("git_push")` raises because the whitelist requires at
  least one arg.
- **F8**: `request("git_push", args=["main"])` is interpreted as
  `git push main` where `main` is a *remote* name — not a branch.

`RelayClient.push()` builds the args correctly: the default is
`["origin", "HEAD"]`. Override either component with the keyword
arguments.

For fully-manual control, import the low-level function:

```python
from aexp.airgapped import request

result = request(
    "git_status",
    ssh_host="cluster-login",
    remote_repo="~/electricrag",
    timeout=30.0,
)
```

## CLI

The same surface is a subcommand group, reachable as `aexp airgapped ...`
(or `python -m aexp.airgapped ...`):

```bash
aexp airgapped status        # ssh <host> true — connectivity check
aexp airgapped pull
aexp airgapped fetch
aexp airgapped rebase
aexp airgapped repo-status   # git status --porcelain=v2
aexp airgapped push --branch feature/x --remote origin
aexp airgapped wandb-sync --approve
```

Every command accepts `--ssh-host`, `--remote-repo`, `--timeout`,
`--connect-timeout`, and `--audit-log` (all optional; they fall back to
the env vars). The CLI exits with the remote command's return code.

## MCP tools

When the `aexp` MCP server is running (it runs on your local machine —
the same place the SSH originates), the relay is also exposed as typed
tools:

```
mcp__aexp__airgapped_status
mcp__aexp__airgapped_pull
mcp__aexp__airgapped_fetch
mcp__aexp__airgapped_repo_status
mcp__aexp__airgapped_rebase
mcp__aexp__airgapped_push
mcp__aexp__airgapped_wandb_sync
```

Each returns a typed dict. `ok` reports whether the SSH round-trip
succeeded — it is `True` even when `returncode` is non-zero (git ran and
reported a result, e.g. a merge conflict). `ok` is `False` only for
transport / validation / consent failures, with `code` naming the error.
Set `ssh_host` / `remote_repo` in the `.mcp.json` `env` block.

## Result + error types

A completed call returns a `RelayResult`:

```python
@dataclass
class RelayResult:
    request_id: str       # uuid; also the audit-log correlator
    op: str               # e.g. "git_pull"
    returncode: int       # remote command's exit code; non-zero is NOT an exception
    stdout: str           # merged stdout+stderr from the login-node run
    duration_s: float     # wall-clock time of the ssh call
```

A non-zero `returncode` is *not* an exception — the relay round-trip
succeeded and the client surfaces git's result as-is. Inspect `r.stdout`.

> **`ssh` failure vs git failure.** `ssh(1)` reserves exit code **255**
> for its *own* transport-layer errors; on a successful connection it
> returns the remote command's exit code instead. So a 255 unambiguously
> means the local→sibling SSH itself failed and is raised as
> `RelayDownError`. Any other non-zero code is git's own result (e.g. the
> login node's git failing to reach GitHub exits 128) and is returned in
> the `RelayResult`, *not* raised.

Protocol-level failures raise subclasses of `RelayError`:

| Exception                | Meaning                                                       |
| ------------------------ | ------------------------------------------------------------- |
| `RelayDownError`         | SSH could not reach the login node (unreachable / auth / host key / missing `ssh`). |
| `RelayValidationError`   | Bad op / regex / args, or `ssh_host`/`remote_repo` not set.   |
| `RelayRejectedError`     | A consent-required op was called without `approve=True`.      |
| `RelayTimeoutError`      | The command did not finish within `timeout`.                  |

## MFA and connection reuse

SSH to a secure login node usually requires MFA. An unattended agent
cannot answer an MFA challenge on every call, so use SSH **connection
multiplexing** — authenticate once per session, reuse the connection:

```
# ~/.ssh/config
Host cluster-login
    HostName login.cluster.example
    User myuser
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
```

Open one master connection interactively at the start of a session
(`ssh cluster-login`, complete the MFA), and every subsequent relay call
multiplexes over it with no re-auth.

> **Windows caveat.** Windows OpenSSH has historically had incomplete
> `ControlMaster` support. If `ssh -O check cluster-login` does not work
> on your build, the fallback is simply to **keep one interactive `ssh`
> session open** in another terminal for the duration of your work —
> while it is alive, the relay's `ssh` calls succeed without prompting.
> (Running the relay from WSL, which has full OpenSSH, also works.)

If a relay call hangs and then raises `RelayTimeoutError`, the usual
cause is no live master connection plus an MFA prompt the agent can't
see — open the master connection and retry.

## Consent

`wandb_sync` publishes run data to W&B, so it is gated: `request()` /
`RelayClient.request()` require `approve=True`, the CLI requires
`--approve`, and the MCP tool requires `approve=True`. Without it the
call is rejected (`RelayRejectedError`) before any SSH happens.

This is a **soft gate**: the caller technically controls the flag, so an
autonomous agent *could* set it. Treat it as a "confirm with the user
first" checkpoint rather than a hard barrier. The compensating control
is the **audit log**.

## Troubleshooting

Symptoms-and-fixes for the failure modes that catch people most often:

### `Permission denied (publickey,...)` from aexp, but `ssh <alias>` works for me interactively

**Almost always: your SSH key has a passphrase.** Your interactive shell silently uses `ssh-agent` to provide the unlocked key, but aexp runs `ssh -o BatchMode=yes` which can't prompt for a passphrase and can't reliably reach `ssh-agent` from a child subprocess. The fix is to strip the passphrase:

```bash
ssh-keygen -p -f ~/.ssh/id_ed25519
# Enter your current passphrase, then press Enter twice for an empty new one.
```

For an HPC research-cluster key used for automation, an empty passphrase is the standard practice. The key file's security comes from OS-level perms on your home directory, not from the passphrase.

(If you genuinely need a passphrase for compliance reasons, you'd have to ensure `ssh-agent` is running and has the key loaded — `ssh-add ~/.ssh/id_ed25519` — in *the same environment* aexp runs from. That's fragile across subprocess boundaries and not recommended unless required.)

### aexp's relay calls time out after ~10–60s instead of failing fast

Same root cause as above in disguise: ssh is hanging waiting for a passphrase prompt it can't display in a non-TTY subprocess. Strip the passphrase.

### `Host key verification failed`

You haven't seeded `known_hosts` for this host yet. Connect once interactively to accept the host key:

```bash
ssh <alias>
# Type "yes" when asked about the host key fingerprint, then exit
```

### `ssh: connect to host ... port 22: Connection refused` or `Could not resolve hostname`

Network/DNS issue. Check VPN is up, the cluster hostname is reachable (`ping`), and your `~/.ssh/config` `HostName` matches a real address.

### `RelayValidationError: ssh_host is required` from CLI / Python API

You didn't pass `--ssh-host` and `$AEXP_RELAY_SSH_HOST` isn't set in this shell. Either pass `--ssh-host <alias>` explicitly, or export the env var (`$env:AEXP_RELAY_SSH_HOST = "h4h"` in PowerShell). The MCP tools read this from the `.mcp.json` `env` block instead — different code path.

### MCP tools return `{ok: false, code: "RelayValidationError"}`

The `.mcp.json` `env` block for the `aexp` server doesn't have `AEXP_RELAY_SSH_HOST` and `AEXP_RELAY_REMOTE_REPO`. Run `aexp airgapped init --ssh-host <alias> --remote-repo <path>` to wire them in, then `/mcp` reconnect.

### New tools (`airgapped_*`) don't appear in the MCP tool list

Your MCP server hasn't been restarted since you upgraded aexp. `/mcp` reconnect (or restart Claude Code).

## Audit log

Every relay op appends one line to a local-side log (default
`~/.aexp/airgapped-relay.log`):

```
2026-05-19T14:03:11+00:00 id=4f3a9c1d op=git_pull args=[] rc=0 dur=1.24s
```

It records the op, args, return code, and duration for every call —
including failed and consent-gated ones — so there is a complete record
of what the relay did. A failed audit write never breaks a relay call.

## Setup

> **The sibling node needs almost nothing.** The relay only requires
> `git` to be installed there (it always is on any login node / jumpbox).
> The consent-gated `wandb_sync` op additionally needs `wandb` on the
> sibling node's `PATH` when invoked over SSH. **No Python env, no
> `aexp` install, no daemon, nothing aexp-specific runs on the sibling
> side.** The relay just ssh-runs git, against a clone you keep on the
> shared `$HOME`.

One command + a few manual steps. Assuming you've already run
`aexp install --dev` in your consumer repo (so a `.mcp.json` exists):

```bash
aexp airgapped init --ssh-host h4h --remote-repo /cluster/home/USER/myrepo
```

This:

1. Writes `AEXP_RELAY_SSH_HOST` and `AEXP_RELAY_REMOTE_REPO` into the
   `aexp` MCP server's `env` block in `.mcp.json` (idempotent; safe to
   re-run; pass `--force` to overwrite a different existing value).
2. Prints the `~/.ssh/config` snippet to paste in (auto-editing your
   SSH config is intentionally avoided — your existing host setup is
   personal).

After running it, do the two manual steps it prints:

| # | What | Why |
|---|------|-----|
| 1 | Paste the `Host <alias>` block into `~/.ssh/config` | Names the login node, configures `ControlMaster` for MFA reuse. |
| 2 | `ssh <alias>` once interactively | Seed `known_hosts`, complete MFA. Leaves the `ControlMaster` socket alive for `ControlPersist` (default 8h); subsequent relay calls multiplex over it with no re-auth. |
| 3 | `/mcp` reconnect in Claude Code | Restarts the `aexp` MCP server so the new `airgapped_*` tools register and the env block is read. |
| 4 | `aexp airgapped status` | Verifies SSH connectivity end-to-end. |

If you don't use the MCP tools, you can skip the `.mcp.json` edit and
just set the two env vars in your shell profile; the CLI and Python API
read them the same way.

### SSH authentication (passwordless setup)

The relay runs `ssh` non-interactively (`BatchMode=yes`), so a password
prompt can't be answered — `ssh <alias> true` must succeed **silently**
for the relay to work. If `ssh <alias>` currently prompts you for a
password, set up SSH key authentication first. One-time per cluster:

**1. Check whether you already have an SSH key.**

```powershell
# Windows PowerShell
ls ~/.ssh/*.pub
```
```bash
# Linux / macOS
ls ~/.ssh/*.pub
```

If you see `id_ed25519.pub` (or `id_rsa.pub`), you already have a key —
skip to step 3.

**2. Generate a key if you don't have one.**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

> ⚠️ **Use an empty passphrase.** When prompted, press Enter twice.
> The relay invokes ssh with `BatchMode=yes` so no prompts can be
> answered — a passphrase-protected key will fail with
> `Permission denied (publickey,...)` from aexp even though it works
> in your interactive shell (where `ssh-agent` is caching the unlocked
> key). If you already created a key with a passphrase, strip it:
> `ssh-keygen -p -f ~/.ssh/id_ed25519` (enter old passphrase, then
> press Enter twice for an empty new one).

**3. Copy your public key to the cluster's `~/.ssh/authorized_keys`.**

On Linux / macOS:

```bash
ssh-copy-id <alias>
```

On Windows (no `ssh-copy-id` in OpenSSH for Windows), use the
equivalent one-liner:

```powershell
Get-Content ~/.ssh/id_ed25519.pub | ssh <alias> "umask 077; mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
```

You'll be prompted for the cluster password **one last time** — you're
authenticating in order to append the key. After this, the cluster
trusts the key for future logins.

**4. Verify silent login.**

```bash
ssh <alias> hostname
```

This should print the cluster's hostname with **no password prompt**. If
it does, key auth is working and the relay is good to go.

> **A note on Windows + ControlMaster.** Linux/macOS users sometimes set
> up `ControlMaster` so an authenticated SSH connection is reused across
> commands. Windows OpenSSH's `ControlMaster` support is incomplete and
> often errors out with `getsockname failed: Not a socket`. With
> passwordless key auth, you don't need `ControlMaster` at all — every
> relay call does a quick connect → run → disconnect under a second.
> Just don't include `ControlMaster` lines in your `~/.ssh/config` on
> Windows.
