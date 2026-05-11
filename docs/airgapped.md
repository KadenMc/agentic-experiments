# Airgapped relay (`aexp.airgapped`)

A file-based bridge between a no-internet compute node and an
internet-having login node — designed for secure HPC sites where the
agent's runtime is network-isolated but a sibling node sharing
``$HOME`` has outbound network.

This is opt-in infrastructure. Most agentic-experiments users don't need
it; the module is **not** imported by `aexp` at package init. Import
`aexp.airgapped` only when your target compute is genuinely airgapped.

## The problem this solves

A common HPC topology, especially at clinical / government / regulated
sites:

- **Compute nodes** have GPUs but no outbound internet.
- **Login nodes** have internet but no GPUs.
- The two nodes share the user's ``$HOME`` filesystem.
- SSH from the agent's runtime to the cluster is forbidden by policy.

The agent can run code on the compute node (via a Jupyter kernel reached
through a port-forward, an MCP server bound to the kernel, or similar),
but it cannot ``git pull``, ``git push``, or ``wandb sync`` because those
need network. The relay closes that gap without requiring the agent to
SSH anywhere.

## How it works

A small daemon under ``tmux`` on the login node watches a queue
directory (``~/.relay/`` by default) for request files. The client on
the compute node drops a JSON request into ``inbox/`` via atomic
rename; the daemon picks it up, runs the whitelisted command, and
writes a JSON response into ``outbox/``. The client polls and returns
the result.

```
compute node                   shared $HOME                  login node
(no internet)                   filesystem                    (internet)
─────────────                  ──────────────                ─────────────
                              ~/.relay/
RelayClient                    inbox/  ─────►  Daemon (tmux)
  .pull()       writes JSON                      runs `git pull --ff-only`
  .push()                                        writes response
  .status()                    outbox/ ◄────
                  reads JSON
returns RelayResult
```

Three pieces guarantee correctness on shared, possibly-laggy filesystems
(networked $HOME):

- **Atomic rename via ``.tmp`` sibling.** Writes go to ``<path>.tmp``
  first, then ``Path.replace(<path>)``. POSIX-atomic; NTFS-atomic for
  non-shared opens.
- **Poll, not ``inotify``.** Networked-FS event mechanisms are
  unreliable cross-node; ``aexp.airgapped`` polls at 250ms (client) /
  500ms (daemon).
- **Heartbeat file.** Daemon writes ``~/.relay/heartbeat`` every 5s; the
  client raises ``RelayDownError`` if the heartbeat is missing or older
  than 30s.

The whole protocol — including consent gating for sensitive ops,
GC of old outbox / log files, stale-processing recovery, and
operator-stop fingerprinting — has 56 tests in the upstream electricrag
reference implementation; see "Provenance" at the bottom of this page.

## Whitelist

Only a fixed set of operations is allowed. The whitelist lives in
:data:`aexp.airgapped.ALLOWED`:

| Op           | Command (daemon-side)             | Consent | Per-call args              |
| ------------ | --------------------------------- | ------- | -------------------------- |
| `git_pull`   | `git pull --ff-only`              | auto    | none                       |
| `git_push`   | `git push <args>`                 | auto    | `^[a-zA-Z0-9._/\-]+$` ×N   |
| `git_fetch`  | `git fetch --all --prune`         | auto    | none                       |
| `git_status` | `git status --porcelain=v2`       | auto    | none                       |
| `git_rebase` | `git pull --rebase`               | auto    | none                       |
| `wandb_sync` | `wandb sync --sync-all`           | **user** | none                      |

Auto-approved ops run immediately on the daemon. Consent-required ops
park in ``pending/`` until the user explicitly approves them by running
``relay-approve <uuid>`` (a small shell helper) on the login node —
``relay-reject <uuid>`` is the other side of that gate.

The whitelist is closed by design. There is no escape hatch for
arbitrary commands; **all** request shapes pass through
:func:`aexp.airgapped.validate_request`, which enforces:

- Op name is in ``ALLOWED``.
- ``args`` is a list of strings; each arg matches the per-op regex if
  one is set; max 32 args, max 256 chars per arg.
- ``cwd`` is required (explicit on every call) and resolves under
  ``$HOME``. An optional ``AEXP_RELAY_CWD_NAMES`` env var further
  restricts the allowed top-level dir names under ``$HOME``.

If you need a new op, extend ``ALLOWED`` and add the appropriate regex
— don't try to smuggle arbitrary commands through the existing entries.

## Client API

The recommended entry point is :class:`aexp.airgapped.RelayClient`:

```python
from aexp.airgapped import RelayClient

relay = RelayClient()        # cwd=Path.cwd(), queue=~/.relay
r = relay.pull()
print(r.returncode, r.stdout)
```

The client takes three optional constructor arguments:

| Param            | Default          | Notes                                          |
| ---------------- | ---------------- | ---------------------------------------------- |
| `queue`          | `~/.relay`       | Override if the daemon was launched elsewhere. |
| `cwd`            | `Path.cwd()`     | Daemon `cd`s here before running the command.  |
| `default_timeout`| `60.0` s         | Per-call timeout for auto-approved ops.        |

Five git verbs are exposed as dedicated methods, plus a generic escape
hatch:

```python
relay.pull()                            # git pull --ff-only
relay.fetch()                           # git fetch --all --prune
relay.status()                          # git status --porcelain=v2
relay.rebase()                          # git pull --rebase
relay.push()                            # git push origin HEAD  (designed-out F7/F8)
relay.push(branch="feature/x")          # git push origin feature/x
relay.push(branch="x", remote="fork")   # git push fork x
relay.request("wandb_sync", timeout=900) # consent-required op
```

Why dedicated methods for git? The raw ``request("git_push")`` call has
two arg-ordering frictions documented as F7/F8 in the electricrag
session that motivated this port:

- **F7**: ``request("git_push")`` raises because the whitelist requires
  at least one arg (the regex is non-empty).
- **F8**: ``request("git_push", args=["main"])`` is interpreted as
  ``git push main`` where ``main`` is a *remote* name — not a branch.

``RelayClient.push()`` builds the args correctly: the default is
``["origin", "HEAD"]``, which pushes the currently checked-out branch to
the matching upstream. Override either component with the keyword
arguments.

For non-git ops, use the generic ``.request()`` method (it just calls
the underlying :func:`aexp.airgapped.request`):

```python
relay.request("wandb_sync", timeout=900.0)
```

Or import the raw function if you want fully-manual control:

```python
from aexp.airgapped import request

result = request(
    "git_status",
    queue=Path("~/my-relay").expanduser(),
    cwd=str(Path.cwd()),
    timeout=30.0,
)
```

## Result + error types

A successful call returns a :class:`aexp.airgapped.RelayResult`:

```python
@dataclass
class RelayResult:
    request_id: str       # uuid; matches the inbox/outbox/log file stem
    op: str               # e.g. "git_pull"
    returncode: int       # subprocess exit code; non-zero is NOT an exception
    stdout: str           # merged stdout+stderr from the daemon-side run
    duration_s: float     # wall-clock time on the daemon side
```

Non-zero ``returncode`` is *not* an exception. The daemon ran the
command and got a result; the client surfaces it as-is so the caller can
decide whether (e.g.) a merge conflict is fatal. Inspect ``r.stdout``
for what git actually said.

Protocol-level failures raise subclasses of
:class:`aexp.airgapped.RelayError`:

| Exception                | Meaning                                                       |
| ------------------------ | ------------------------------------------------------------- |
| `RelayDownError`         | Daemon heartbeat missing or stale (>30s).                     |
| `RelayValidationError`   | Daemon rejected the request (bad op / regex / args / cwd).    |
| `RelayRejectedError`     | User touched `rejected/<uuid>` for a consent-required op.     |
| `RelayTimeoutError`      | Client's per-call timeout elapsed before a response arrived.  |
| `RelayCrashedError`      | Daemon died mid-execution; outbox synthesized on next start.  |

## Daemon bootstrap (login node)

On the login node, in a one-time setup:

```bash
# 1. Install the package on the login node too (the daemon imports
#    `aexp.airgapped._relay`).
pip install agentic-experiments

# 2. Optionally install the small approve/reject shell helpers into
#    ~/.relay/_bin/ and add that to PATH.
python -m aexp.airgapped install-helpers
export PATH="$HOME/.relay/_bin:$PATH"      # add to .bashrc

# 3. Launch the daemon under tmux (survives logout; restarts trivially).
tmux new -d -s relay 'python -m aexp.airgapped daemon --log ~/.relay/daemon.log'

# 4. Verify it's healthy.
python -m aexp.airgapped status
# heartbeat: fresh (1.2s old, pid=12345)
# inbox: 0  processing: 0  pending: 0
```

The daemon is single-process, single-threaded, and idempotent under
restart — kill it and re-launch any time. Pending consent requests
survive a daemon restart; in-flight processing is recovered on the next
startup via a sweep of ``processing/``.

> **Subcommands.** ``python -m aexp.airgapped`` exposes `daemon`,
> `install-helpers`, and `status`. They all accept a shared
> ``--queue PATH`` (default ``~/.relay``). The daemon also accepts
> ``--log PATH`` for an optional file handler.

## Cwd allowlist (optional hardening)

The default policy is "any subdir of ``$HOME`` is allowed." If you want
to lock the daemon down to a fixed set of project directories, set the
``AEXP_RELAY_CWD_NAMES`` env var **on the daemon process**:

```bash
AEXP_RELAY_CWD_NAMES="electricrag,myotherrepo" \
    tmux new -d -s relay 'python -m aexp.airgapped daemon'
```

A request whose ``cwd`` doesn't resolve to a top-level dir matching one
of those names is rejected with ``RelayValidationError``. The check
runs in addition to the always-on under-``$HOME`` enforcement.

## End-to-end: agent-on-compute pulls latest, runs, pushes results

```python
from pathlib import Path
from aexp.airgapped import RelayClient

# Compute-node Jupyter cell — kernel has no internet.
relay = RelayClient(cwd=Path("~/electricrag").expanduser())

# 1. Sync latest code from the laptop / GitHub.
r = relay.pull()
assert r.returncode == 0, r.stdout

# 2. Run the experiment locally (this part doesn't need the relay).
#    ... training code ...

# 3. Commit results to the local clone (still no internet needed).
import subprocess
subprocess.run(["git", "add", "outputs/"], check=True, cwd=relay.cwd)
subprocess.run(["git", "commit", "-m", "results"], check=True, cwd=relay.cwd)

# 4. Push back through the daemon.
r = relay.push()  # → git push origin HEAD
assert r.returncode == 0, r.stdout

# 5. Optional: sync wandb offline runs (consent-required).
#    The user must `relay-approve <uuid>` on the login node within
#    `timeout` seconds, or the call raises RelayTimeoutError.
r = relay.request("wandb_sync", timeout=900.0)
```

## Provenance

The reference implementation lives upstream in
[electricrag/dev/relay.py](https://github.com/KadenMc/electricrag) (preserved alongside
the agentic-experiments port) with a 56-test suite covering daemon
lifecycle, consent state machine, heartbeat staleness recovery, GC of
old outbox / log files, and stale-processing recovery. The aexp port's
``tests/test_airgapped.py`` is a port-level smoke (30 tests) over the
public surface — for the exhaustive behavioral spec, see the upstream
suite.

Two specific design decisions the upstream session crystallized that the
aexp version preserves verbatim:

- **Closed whitelist + per-op regex.** No "advanced mode," no escape
  hatch, no string interpolation that could leak shell metacharacters.
  The test ``test_validate_shell_injection_in_push_arg_raises`` pins this
  invariant.
- **Explicit `cwd` per call.** The earliest electricrag implementation
  defaulted ``cwd`` to a hard-coded ``~/electricrag``; the port removes
  that default. ``RelayClient`` fills in ``Path.cwd()`` at construction
  time so callers don't usually notice, but the underlying
  ``validate_request`` requires the field — there is no project-specific
  default baked into the surface.
