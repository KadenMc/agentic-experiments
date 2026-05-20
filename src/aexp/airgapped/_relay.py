"""Direct-SSH bridge to an internet-having sibling node.

Runs whitelisted git / wandb commands on a sibling node (login node on
an HPC, jumpbox elsewhere) on behalf of an agent whose compute machine
is network-isolated. The agent (and this module) run on the user's
local machine (where Claude Code runs); that local machine reaches the
sibling node by SSH; the sibling node has internet and shares ``$HOME``
with the airgapped compute, so the same git clone is visible to both.

A per-call ``ssh`` invocation -- no daemon, no queue, no heartbeat.

Transport
---------
:func:`request` runs ``ssh <host> "cd <repo> && <whitelisted argv>"`` via
``subprocess`` and returns a :class:`RelayResult`.

Whitelist
---------
Auto-approved: ``git_pull``, ``git_push``, ``git_fetch``, ``git_status``,
``git_rebase``. Consent-required (caller must pass ``approve=True``):
``wandb_sync``.

Security
--------
- Closed whitelist; per-op ``args_regex`` excludes shell metacharacters.
- Every token of the remote command is ``shlex.quote``-d for the remote
  POSIX shell.
- ``ssh`` runs with ``BatchMode=yes`` so it never blocks on a prompt
  (host-key or password): a missing ``known_hosts`` entry fails fast
  rather than hanging an unattended agent.

See ``docs/airgapped.md`` for setup (the ``~/.ssh/config`` host alias,
ControlMaster for MFA reuse) and the full protocol.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from aexp.utils.atomic import atomic_write

log = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

DEFAULT_CLIENT_TIMEOUT_S = 60.0          # auto-approved ops
DEFAULT_CONSENT_TIMEOUT_S = 600.0        # consent-required ops (wandb sync is slow)
DEFAULT_CONNECT_TIMEOUT_S = 10.0         # ssh -o ConnectTimeout

ENV_SSH_HOST = "AEXP_RELAY_SSH_HOST"
ENV_REMOTE_REPO = "AEXP_RELAY_REMOTE_REPO"
ENV_AUDIT_LOG = "AEXP_RELAY_AUDIT_LOG"
ENV_SSH_VERBOSE = "AEXP_RELAY_SSH_VERBOSE"  # if truthy, pass -vv to ssh (diagnostic)

DEFAULT_AUDIT_LOG = Path.home() / ".aexp" / "airgapped-relay.log"

MAX_ARG_LENGTH = 256
MAX_ARGS = 32

# ssh(1) reserves exit code 255 for its own transport-layer failures
# (connection refused, host unresolved/unreachable, auth or host-key
# failure, connect timeout). On a *successful* connection it returns the
# remote command's exit code instead -- so 255 unambiguously means the
# local->sibling SSH itself failed, not git.
SSH_TRANSPORT_RC = 255

# ============================================================================
# Whitelist
# ============================================================================


@dataclass(frozen=True)
class OpSpec:
    """One whitelisted operation.

    Parameters
    ----------
    argv : list of str
        Base argv prepended to any per-request ``args``.
    consent : bool
        If True, the op is outward-facing and the caller must pass
        ``approve=True`` (``--approve`` on the CLI) to authorize it.
        Without that, :func:`request` raises before any SSH call.
    args_regex : str or None
        If set, every per-request arg must ``re.fullmatch`` this regex.
        If None, no per-request args are accepted.
    """

    argv: list[str]
    consent: bool
    args_regex: str | None = None


ALLOWED: dict[str, OpSpec] = {
    # Auto-approved (auditable + reversible by their nature)
    "git_pull":   OpSpec(["git", "pull", "--ff-only"], consent=False),
    "git_push":   OpSpec(["git", "push"], consent=False, args_regex=r"^[a-zA-Z0-9._/\-]+$"),
    "git_fetch":  OpSpec(["git", "fetch", "--all", "--prune"], consent=False),
    "git_status": OpSpec(["git", "status", "--porcelain=v2"], consent=False),
    "git_rebase": OpSpec(["git", "pull", "--rebase"], consent=False),
    # Consent-required (caller must pass approve=True)
    "wandb_sync": OpSpec(["wandb", "sync", "--sync-all"], consent=True),
}


# ============================================================================
# Errors
# ============================================================================


class RelayError(RuntimeError):
    """Base for all relay errors raised by the client."""


class RelayDownError(RelayError):
    """SSH could not reach the login node.

    Covers connection refused, host unresolved/unreachable, auth or
    host-key failure, connect timeout, and a missing ``ssh`` binary.
    Distinct from a non-zero git exit code, which is returned in a
    :class:`RelayResult` rather than raised.
    """


class RelayValidationError(RelayError):
    """A request is invalid (unknown op, bad args, or missing config)."""


class RelayRejectedError(RelayError):
    """A consent-required op was called without ``approve=True``."""


class RelayTimeoutError(RelayError):
    """The SSH command did not complete within the client timeout."""


# ============================================================================
# Result type
# ============================================================================


@dataclass
class RelayResult:
    """Return value of a completed request.

    ``returncode`` is the remote command's exit code; a non-zero value is
    *not* an exception -- the relay round-trip succeeded and the client
    surfaces the result as-is so the caller can decide whether (e.g.) a
    merge conflict is fatal. SSH-transport failures raise
    :class:`RelayDownError` instead of returning a ``RelayResult``.
    """

    request_id: str
    op: str
    returncode: int
    stdout: str
    duration_s: float


# ============================================================================
# Validation
# ============================================================================


def validate_request(op: str, args: list[str] | None) -> tuple[str, list[str]]:
    """Validate an op + args pair against the whitelist.

    Parameters
    ----------
    op : str
        Operation name; must be a key of :data:`ALLOWED`.
    args : list of str or None
        Per-request arguments. Required for ops whose ``args_regex`` is
        set; must be empty/None otherwise.

    Returns
    -------
    op : str
        The validated operation name.
    args : list of str
        The validated argument list (normalized from None to ``[]``).

    Raises
    ------
    RelayValidationError
        If the op is unknown, or args are missing/malformed/over-limit
        or fail the per-op regex.
    """
    if not isinstance(op, str) or op not in ALLOWED:
        raise RelayValidationError(
            f"unknown op: {op!r}; allowed: {sorted(ALLOWED)}"
        )

    args = list(args or [])
    if len(args) > MAX_ARGS:
        raise RelayValidationError(f"too many args ({len(args)} > {MAX_ARGS})")

    spec = ALLOWED[op]
    if spec.args_regex is None:
        if args:
            raise RelayValidationError(
                f"op {op!r} accepts no per-request args; got {args!r}"
            )
    else:
        if not args:
            raise RelayValidationError(
                f"op {op!r} requires at least one arg (regex set)"
            )
        for a in args:
            if not isinstance(a, str):
                raise RelayValidationError(
                    f"arg must be str, got {type(a).__name__}: {a!r}"
                )
            if len(a) > MAX_ARG_LENGTH:
                raise RelayValidationError(
                    f"arg too long ({len(a)} > {MAX_ARG_LENGTH})"
                )
            if not re.fullmatch(spec.args_regex, a):
                raise RelayValidationError(
                    f"arg failed regex {spec.args_regex!r}: {a!r}"
                )
    return op, args


# ============================================================================
# Helpers
# ============================================================================


def _resolve_config(
    ssh_host: str | None, remote_repo: str | None,
) -> tuple[str, str]:
    """Resolve ``ssh_host`` / ``remote_repo`` from args, falling back to env."""
    host = (ssh_host or os.environ.get(ENV_SSH_HOST, "")).strip()
    repo = (remote_repo or os.environ.get(ENV_REMOTE_REPO, "")).strip()
    if not host:
        raise RelayValidationError(
            f"ssh_host is required: pass ssh_host=... or set ${ENV_SSH_HOST}. "
            "It should name an ~/.ssh/config Host alias (auth/keys/MFA live "
            "in your SSH config, not here)."
        )
    if not repo:
        raise RelayValidationError(
            f"remote_repo is required: pass remote_repo=... or set "
            f"${ENV_REMOTE_REPO}. It is the absolute path of the git repo "
            "on the login node."
        )
    return host, repo


def _resolve_audit_log(audit_log: Path | str | None) -> Path:
    """Resolve the audit-log path: explicit arg, then env, then default."""
    if audit_log is not None:
        return Path(audit_log).expanduser()
    env = os.environ.get(ENV_AUDIT_LOG, "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_AUDIT_LOG


def _build_remote_command(remote_repo: str, op: str, args: list[str]) -> str:
    """Build the shell command string executed on the login node.

    Every token is ``shlex.quote``-d for the remote POSIX shell. Per-request
    args have already passed the per-op regex (no shell metacharacters);
    quoting is defense-in-depth and is what makes a ``remote_repo`` path
    containing spaces safe.
    """
    argv = list(ALLOWED[op].argv) + list(args)
    quoted = " ".join(shlex.quote(tok) for tok in argv)
    return f"cd {shlex.quote(remote_repo)} && {quoted}"


def _is_ssh_transport_failure(returncode: int) -> bool:
    """True if the exit code indicates an SSH-transport (not git) failure.

    See :data:`SSH_TRANSPORT_RC` -- ssh(1) returns 255 only for its own
    errors; any other code is the remote command's own exit status.
    """
    return returncode == SSH_TRANSPORT_RC


def _tail(text: str, n: int) -> str:
    """Return the last ``n`` characters of ``text`` (stripped), with an ellipsis."""
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


def _append_audit(
    audit_log: Path, request_id: str, op: str, args: list[str],
    returncode: int, duration_s: float,
) -> None:
    """Append one line to the local-side audit log. Never raises."""
    line = (
        "{ts} id={rid} op={op} args={args} rc={rc} dur={dur:.2f}s\n".format(
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
            rid=request_id[:8], op=op, args=args, rc=returncode, dur=duration_s,
        )
    )
    try:
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        with audit_log.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:  # pragma: no cover -- audit must never break a call
        log.warning("could not write audit log %s: %s", audit_log, exc)


def _ssh_argv(host: str, connect_timeout: float, remote_command: str) -> list[str]:
    """Build the ``ssh`` argv with the fixed hardening options.

    If ``$AEXP_RELAY_SSH_VERBOSE`` is set to a truthy value, prepends
    ``-vv`` so the subprocess captures ssh's verbose log -- useful when
    diagnosing why a call hung or failed in a non-interactive context
    (e.g. an MCP server's subprocess).
    """
    argv = [
        "ssh",
        # -n: never read from stdin. The relay runs a fixed remote command
        # and wants only its output; without -n, ssh inherits the caller's
        # stdin and -- if that is a pipe that never closes (e.g. an MCP
        # server's stdio transport) -- ssh stays alive after the remote
        # command finishes, hanging the subprocess until the timeout.
        "-n",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(connect_timeout)}",
    ]
    if os.environ.get(ENV_SSH_VERBOSE, "").strip().lower() in ("1", "true", "yes"):
        argv.insert(1, "-vv")
    argv.extend([host, remote_command])
    return argv


def _decode_stderr(raw: bytes | str | None) -> str:
    """Decode subprocess stderr (which may be bytes if TimeoutExpired)."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


# ============================================================================
# Connectivity check
# ============================================================================


def check_connection(
    *,
    ssh_host: str | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> str:
    """Verify the login node is reachable over SSH (``ssh <host> true``).

    Returns the resolved host on success; raises :class:`RelayDownError`
    if the connection fails, or :class:`RelayValidationError` if no host
    is configured.
    """
    host = (ssh_host or os.environ.get(ENV_SSH_HOST, "")).strip()
    if not host:
        raise RelayValidationError(
            f"ssh_host is required: pass ssh_host=... or set ${ENV_SSH_HOST}."
        )
    argv = _ssh_argv(host, connect_timeout, "true")
    overall_timeout = connect_timeout + 10.0
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=overall_timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise RelayDownError(
            f"ssh to {host!r} timed out after {overall_timeout:.0f}s. "
            f"stderr tail: {_tail(_decode_stderr(exc.stderr), 600)}"
        ) from exc
    except FileNotFoundError as exc:
        raise RelayDownError(
            "`ssh` was not found on PATH. Install the OpenSSH client."
        ) from exc
    if proc.returncode != 0:
        raise RelayDownError(
            f"ssh to {host!r} failed (rc={proc.returncode}). "
            f"stderr: {_tail(proc.stderr, 400)}"
        )
    return host


# ============================================================================
# Client
# ============================================================================


def request(
    op: str,
    args: list[str] | None = None,
    *,
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    approve: bool = False,
    timeout: float | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
    audit_log: Path | str | None = None,
) -> RelayResult:
    """Run one whitelisted op on the login node over SSH; block for the result.

    Parameters
    ----------
    op : str
        One of the keys in :data:`ALLOWED`.
    args : list of str, optional
        Per-request arguments. Required for ops whose ``args_regex`` is
        set; must be empty/None otherwise.
    ssh_host : str, optional
        SSH host (ideally an ``~/.ssh/config`` alias). Falls back to
        ``$AEXP_RELAY_SSH_HOST``.
    remote_repo : str, optional
        Absolute path of the git repo on the login node. Falls back to
        ``$AEXP_RELAY_REMOTE_REPO``.
    approve : bool, optional
        Must be True for consent-required ops (``wandb_sync``).
    timeout : float, optional
        Client-side timeout in seconds. Defaults to 60s for auto-approved
        ops, 600s for consent-required ops.
    connect_timeout : float, optional
        ``ssh -o ConnectTimeout`` value.
    audit_log : Path or str, optional
        Where to append the one-line audit record. Falls back to
        ``$AEXP_RELAY_AUDIT_LOG`` then ``~/.aexp/airgapped-relay.log``.

    Returns
    -------
    RelayResult
        The remote command's result. A non-zero ``returncode`` is *not*
        an exception.

    Raises
    ------
    RelayValidationError
        Unknown op, bad args, or missing ssh_host/remote_repo config.
    RelayRejectedError
        A consent-required op was called without ``approve=True``.
    RelayDownError
        The SSH transport itself failed (unreachable, auth, host key).
    RelayTimeoutError
        The command did not finish within ``timeout``.
    """
    op, args = validate_request(op, args)
    spec = ALLOWED[op]
    if spec.consent and not approve:
        raise RelayRejectedError(
            f"op {op!r} is consent-required; pass approve=True (--approve) to "
            "authorize it. Confirm with the user before doing so -- it is an "
            "outward-facing operation."
        )

    host, repo = _resolve_config(ssh_host, remote_repo)
    if timeout is None:
        timeout = DEFAULT_CONSENT_TIMEOUT_S if spec.consent else DEFAULT_CLIENT_TIMEOUT_S
    audit_path = _resolve_audit_log(audit_log)

    remote_cmd = _build_remote_command(repo, op, args)
    argv = _ssh_argv(host, connect_timeout, remote_cmd)
    request_id = uuid.uuid4().hex
    log.debug("relay %s: ssh %s %r", request_id[:8], host, remote_cmd)

    t_start = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - t_start
        _append_audit(audit_path, request_id, op, args, -1, duration)
        stderr_tail = _tail(_decode_stderr(exc.stderr), 800)
        raise RelayTimeoutError(
            f"ssh to {host!r} did not finish within {timeout:.0f}s for op "
            f"{op!r}. stderr tail (partial):\n{stderr_tail}\n"
            "If this is the session's first connection it may be waiting on "
            "MFA -- open an interactive `ssh` session first so subsequent "
            f"calls reuse it. Or set ${ENV_SSH_VERBOSE}=1 in the env to "
            "get ssh -vv output on the next attempt."
        ) from exc
    except FileNotFoundError as exc:
        raise RelayDownError(
            "`ssh` was not found on PATH. Install the OpenSSH client "
            "(Windows: Settings -> Optional features -> OpenSSH Client)."
        ) from exc
    duration = time.monotonic() - t_start

    rc = proc.returncode
    # git writes progress + conflict text to stderr; merge the streams so
    # RelayResult.stdout carries everything the caller needs to inspect.
    merged = (proc.stdout or "") + (proc.stderr or "")
    _append_audit(audit_path, request_id, op, args, rc, duration)

    if _is_ssh_transport_failure(rc):
        raise RelayDownError(
            f"ssh to {host!r} failed (rc={rc}) -- an SSH transport failure, "
            f"not a git error. stderr tail:\n{_tail(proc.stderr, 500)}\n"
            "Check: the host alias in ~/.ssh/config, known_hosts seeded "
            "(connect interactively once), VPN/network, and MFA / ControlMaster."
        )

    return RelayResult(
        request_id=request_id,
        op=op,
        returncode=rc,
        stdout=merged,
        duration_s=round(duration, 4),
    )


# ============================================================================
# CLI
# ============================================================================

airgapped_app = typer.Typer(
    help=(
        "Run whitelisted git/wandb commands on an internet-having sibling "
        "node over SSH -- the bridge for an airgapped compute machine."
    ),
    no_args_is_help=True,
)


def _emit(result: RelayResult, op: str) -> None:
    """Print a RelayResult to stdout and exit with the remote returncode."""
    body = result.stdout.rstrip()
    if body:
        typer.echo(body)
    typer.echo(
        f"[aexp.airgapped] {op} -> rc={result.returncode} "
        f"({result.duration_s:.2f}s)"
    )
    raise typer.Exit(code=result.returncode)


def _run_op(
    op: str,
    args: list[str] | None,
    *,
    ssh_host: str | None,
    remote_repo: str | None,
    timeout: float | None,
    connect_timeout: float,
    audit_log: str | None,
    approve: bool = False,
) -> None:
    """Shared body for the per-op CLI commands."""
    try:
        result = request(
            op, args,
            ssh_host=ssh_host, remote_repo=remote_repo, approve=approve,
            timeout=timeout, connect_timeout=connect_timeout,
            audit_log=audit_log,
        )
    except RelayError as exc:
        typer.echo(f"[aexp.airgapped] {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _emit(result, op)


@airgapped_app.command("status")
def _cli_status(
    ssh_host: str | None = typer.Option(
        None, "--ssh-host", help=f"SSH host alias; default ${ENV_SSH_HOST}."
    ),
    connect_timeout: float = typer.Option(
        DEFAULT_CONNECT_TIMEOUT_S, "--connect-timeout",
        help="SSH connect timeout (seconds).",
    ),
) -> None:
    """Check that the login node is reachable over SSH (`ssh <host> true`)."""
    try:
        host = check_connection(ssh_host=ssh_host, connect_timeout=connect_timeout)
    except RelayError as exc:
        typer.echo(f"[aexp.airgapped] login node UNREACHABLE: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"[aexp.airgapped] login node reachable: {host}")


@airgapped_app.command("pull")
def _cli_pull(
    ssh_host: str | None = typer.Option(None, "--ssh-host"),
    remote_repo: str | None = typer.Option(None, "--remote-repo"),
    timeout: float | None = typer.Option(None, "--timeout"),
    connect_timeout: float = typer.Option(DEFAULT_CONNECT_TIMEOUT_S, "--connect-timeout"),
    audit_log: str | None = typer.Option(None, "--audit-log"),
) -> None:
    """git pull --ff-only on the login node."""
    _run_op(
        "git_pull", None, ssh_host=ssh_host, remote_repo=remote_repo,
        timeout=timeout, connect_timeout=connect_timeout, audit_log=audit_log,
    )


@airgapped_app.command("fetch")
def _cli_fetch(
    ssh_host: str | None = typer.Option(None, "--ssh-host"),
    remote_repo: str | None = typer.Option(None, "--remote-repo"),
    timeout: float | None = typer.Option(None, "--timeout"),
    connect_timeout: float = typer.Option(DEFAULT_CONNECT_TIMEOUT_S, "--connect-timeout"),
    audit_log: str | None = typer.Option(None, "--audit-log"),
) -> None:
    """git fetch --all --prune on the login node."""
    _run_op(
        "git_fetch", None, ssh_host=ssh_host, remote_repo=remote_repo,
        timeout=timeout, connect_timeout=connect_timeout, audit_log=audit_log,
    )


@airgapped_app.command("rebase")
def _cli_rebase(
    ssh_host: str | None = typer.Option(None, "--ssh-host"),
    remote_repo: str | None = typer.Option(None, "--remote-repo"),
    timeout: float | None = typer.Option(None, "--timeout"),
    connect_timeout: float = typer.Option(DEFAULT_CONNECT_TIMEOUT_S, "--connect-timeout"),
    audit_log: str | None = typer.Option(None, "--audit-log"),
) -> None:
    """git pull --rebase on the login node."""
    _run_op(
        "git_rebase", None, ssh_host=ssh_host, remote_repo=remote_repo,
        timeout=timeout, connect_timeout=connect_timeout, audit_log=audit_log,
    )


@airgapped_app.command("repo-status")
def _cli_repo_status(
    ssh_host: str | None = typer.Option(None, "--ssh-host"),
    remote_repo: str | None = typer.Option(None, "--remote-repo"),
    timeout: float | None = typer.Option(None, "--timeout"),
    connect_timeout: float = typer.Option(DEFAULT_CONNECT_TIMEOUT_S, "--connect-timeout"),
    audit_log: str | None = typer.Option(None, "--audit-log"),
) -> None:
    """git status --porcelain=v2 on the login node's repo."""
    _run_op(
        "git_status", None, ssh_host=ssh_host, remote_repo=remote_repo,
        timeout=timeout, connect_timeout=connect_timeout, audit_log=audit_log,
    )


@airgapped_app.command("push")
def _cli_push(
    branch: str = typer.Option("HEAD", "--branch", help="Branch to push."),
    remote: str = typer.Option("origin", "--remote", help="Remote name."),
    ssh_host: str | None = typer.Option(None, "--ssh-host"),
    remote_repo: str | None = typer.Option(None, "--remote-repo"),
    timeout: float | None = typer.Option(None, "--timeout"),
    connect_timeout: float = typer.Option(DEFAULT_CONNECT_TIMEOUT_S, "--connect-timeout"),
    audit_log: str | None = typer.Option(None, "--audit-log"),
) -> None:
    """git push <remote> <branch> on the login node (default: origin HEAD)."""
    _run_op(
        "git_push", [remote, branch], ssh_host=ssh_host, remote_repo=remote_repo,
        timeout=timeout, connect_timeout=connect_timeout, audit_log=audit_log,
    )


@airgapped_app.command("wandb-sync")
def _cli_wandb_sync(
    approve: bool = typer.Option(
        False, "--approve",
        help="Required: authorize this outward-facing wandb sync.",
    ),
    ssh_host: str | None = typer.Option(None, "--ssh-host"),
    remote_repo: str | None = typer.Option(None, "--remote-repo"),
    timeout: float | None = typer.Option(None, "--timeout"),
    connect_timeout: float = typer.Option(DEFAULT_CONNECT_TIMEOUT_S, "--connect-timeout"),
    audit_log: str | None = typer.Option(None, "--audit-log"),
) -> None:
    """wandb sync --sync-all on the login node (consent-required)."""
    if not approve:
        typer.echo(
            "[aexp.airgapped] wandb-sync is consent-required (it publishes "
            "run data). Re-run with --approve once the user authorizes it.",
            err=True,
        )
        raise typer.Exit(code=2)
    _run_op(
        "wandb_sync", None, ssh_host=ssh_host, remote_repo=remote_repo,
        timeout=timeout, connect_timeout=connect_timeout, audit_log=audit_log,
        approve=True,
    )


# ============================================================================
# `aexp airgapped init` -- one-shot setup helper
# ============================================================================


def init_mcp_config(
    mcp_config_path: Path,
    *,
    ssh_host: str,
    remote_repo: str,
    server_name: str = "aexp",
    force: bool = False,
) -> dict[str, Any]:
    """Wire airgapped env config into an existing ``.mcp.json``.

    Reads the file, locates the named MCP server entry (default
    ``"aexp"``), adds or updates the ``AEXP_RELAY_SSH_HOST`` and
    ``AEXP_RELAY_REMOTE_REPO`` env keys inside that entry's ``env``
    block, and writes the file back atomically.

    Parameters
    ----------
    mcp_config_path : Path
        Path to the ``.mcp.json`` file. Must already exist (typically
        generated by ``aexp install --dev`` in a consumer repo).
    ssh_host : str
        SSH host to use -- ideally a ``~/.ssh/config`` Host alias so
        auth detail (identity file, MFA, ControlMaster) lives there.
    remote_repo : str
        Absolute path of the git repo on the login node.
    server_name : str, optional
        Which ``mcpServers`` entry to update. Defaults to ``"aexp"``.
    force : bool, optional
        Allow overwriting existing env values that differ from the
        requested ones. Without ``force``, a value conflict raises.

    Returns
    -------
    dict
        ``{path, changes, already_correct}`` -- ``changes`` is a list of
        human-readable change descriptions; ``already_correct`` is True
        if no write was needed (idempotent re-run).

    Raises
    ------
    FileNotFoundError
        If ``mcp_config_path`` does not exist.
    ValueError
        If the file is unparseable or has no entry for ``server_name``.
    RuntimeError
        If an env value differs from the requested one and ``force`` is
        False.
    """
    if not mcp_config_path.is_file():
        raise FileNotFoundError(
            f".mcp.json not found at {mcp_config_path}. "
            "Run `aexp install --dev` first to generate one, or pass "
            "--mcp-config /path/to/file."
        )

    try:
        data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{mcp_config_path} is not valid JSON: {exc}"
        ) from exc

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server_name not in servers:
        available = sorted(servers.keys()) if isinstance(servers, dict) else []
        raise ValueError(
            f"mcpServers.{server_name!r} not found in {mcp_config_path}. "
            f"Available servers: {available}. "
            "Run `aexp install --dev` to install the aexp MCP server entry."
        )

    server = servers[server_name]
    if not isinstance(server, dict):
        raise ValueError(
            f"mcpServers.{server_name} is not an object in {mcp_config_path}."
        )
    env = server.setdefault("env", {})
    if not isinstance(env, dict):
        raise ValueError(
            f"mcpServers.{server_name}.env is not an object in {mcp_config_path}."
        )

    changes: list[str] = []
    for key, new_value in ((ENV_SSH_HOST, ssh_host), (ENV_REMOTE_REPO, remote_repo)):
        existing = env.get(key)
        if existing is None:
            env[key] = new_value
            changes.append(f"added {key}={new_value!r}")
        elif existing == new_value:
            continue  # idempotent
        elif not force:
            raise RuntimeError(
                f"{key} already set to {existing!r} in {mcp_config_path}; "
                f"re-run with --force to overwrite to {new_value!r}, or edit "
                "the file manually."
            )
        else:
            env[key] = new_value
            changes.append(f"updated {key}: {existing!r} -> {new_value!r}")

    if changes:
        atomic_write(mcp_config_path, json.dumps(data, indent=2) + "\n")

    return {
        "path": mcp_config_path,
        "changes": changes,
        "already_correct": not changes,
    }


@airgapped_app.command("init")
def _cli_init(
    ssh_host: str = typer.Option(
        ..., "--ssh-host",
        help="SSH host alias to use (ideally an entry in ~/.ssh/config).",
    ),
    remote_repo: str = typer.Option(
        ..., "--remote-repo",
        help="Absolute path of the git repo on the login node.",
    ),
    mcp_config: Path = typer.Option(
        Path(".mcp.json"), "--mcp-config",
        help="Path to .mcp.json. Default: ./.mcp.json (run from the consumer repo).",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite existing AEXP_RELAY_* env values that differ from these.",
    ),
) -> None:
    """One-shot wiring: write airgapped env into .mcp.json + print next steps.

    Edits the ``.mcp.json`` aexp server's env block (idempotent) and prints
    the ``~/.ssh/config`` snippet plus the remaining manual steps (the
    interactive SSH + MFA, the ``/mcp`` reconnect, and the verification).
    """
    try:
        result = init_mcp_config(
            mcp_config_path=mcp_config.expanduser().resolve(),
            ssh_host=ssh_host, remote_repo=remote_repo, force=force,
        )
    except FileNotFoundError as exc:
        typer.echo(f"[aexp.airgapped init] {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(f"[aexp.airgapped init] {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        typer.echo(f"[aexp.airgapped init] {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"[aexp.airgapped init] {result['path']}")
    if result["already_correct"]:
        typer.echo("  (.mcp.json env already matches; nothing to write)")
        typer.echo("")
        typer.echo("To re-print the setup steps, run with --force, or see")
        typer.echo("docs/airgapped.md for the full setup guide.")
        return
    for change in result["changes"]:
        typer.echo(f"  - {change}")

    typer.echo("")
    ssh_config_path = (
        "%USERPROFILE%\\.ssh\\config (e.g. C:\\Users\\<you>\\.ssh\\config)"
        if os.name == "nt" else "~/.ssh/config"
    )
    typer.echo("=" * 72)
    typer.echo("All steps below happen on your LOCAL machine (the one that")
    typer.echo("runs Claude Code), NOT on the cluster.")
    typer.echo("=" * 72)
    typer.echo("")
    typer.echo(f"Step 1. Add this block to {ssh_config_path}")
    typer.echo("        (adjust HostName / User to match your cluster):")
    typer.echo("")
    typer.echo(f"            Host {ssh_host}")
    typer.echo("                HostName <login-node-hostname>")
    typer.echo("                User <your-username>")
    typer.echo("")
    typer.echo(f"Step 2. Ensure passwordless SSH works: `ssh {ssh_host} hostname`")
    typer.echo("        should print the cluster's hostname with NO password")
    typer.echo("        prompt. If it asks for a password, set up SSH key auth:")
    typer.echo("")
    typer.echo("        a) Generate a key (skip if you already have one):")
    typer.echo("")
    typer.echo("              ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519")
    typer.echo("")
    typer.echo("           IMPORTANT: when it asks for a passphrase, press")
    typer.echo("           Enter twice to leave it EMPTY. A passphrase-protected")
    typer.echo("           key cannot be used non-interactively without ssh-agent")
    typer.echo("           and will cause `Permission denied (publickey,...)`.")
    typer.echo("")
    typer.echo("        b) Copy your public key to the cluster:")
    typer.echo("")
    if os.name == "nt":
        typer.echo(
            f"              Get-Content ~/.ssh/id_ed25519.pub | ssh {ssh_host} "
            '"umask 077; mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && '
            'chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"'
        )
    else:
        typer.echo(f"              ssh-copy-id {ssh_host}")
    typer.echo("")
    typer.echo("           (You'll be prompted for the cluster password ONCE.")
    typer.echo("            After this, key auth takes over.)")
    typer.echo("")
    typer.echo(f"        c) Re-test: `ssh {ssh_host} hostname` should now be silent.")
    typer.echo("")
    typer.echo("Step 3. In Claude Code: /mcp  (reconnect the aexp server so")
    typer.echo("        the new .mcp.json env block is read).")
    typer.echo("")
    typer.echo(f"Step 4. Verify: aexp airgapped status --ssh-host {ssh_host}")


# ============================================================================
# Module entry point
# ============================================================================


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m aexp.airgapped``.

    Delegates to the Typer ``airgapped_app``. Returns the process exit
    code so ``__main__.py`` can ``raise SystemExit(main(...))``.
    """
    try:
        airgapped_app(args=argv, standalone_mode=True)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0
