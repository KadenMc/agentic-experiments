"""File-based bridge between a no-internet compute node and an internet-having login node.

Designed for **airgapped compute environments** where the agent's runtime
has no internet access but a sibling node (sharing the user's home
filesystem) does — the canonical case being secure HPC sites where
compute nodes are network-isolated for compliance but login nodes
have outbound internet.

The cluster's compute node (where Jupyter runs and the agent operates) has
no internet access; only the login node does. They share the user's home
directory but not the project directory. SSH from agent to cluster is
forbidden by institutional policy. This module provides a small bridge:

- A **daemon** (``relay daemon``) runs under tmux on the login node,
  polling ``~/.relay/inbox/`` for request files dropped by the agent and
  executing whitelisted commands (git operations, wandb sync) on its
  behalf. Output streams to a per-request log; a final response file in
  ``outbox/`` signals completion.
- A **client** (``relay.request``) is importable from notebook cells on
  the compute node. It writes a request to ``inbox/`` via atomic rename,
  polls ``outbox/`` until the response appears, and returns a
  ``RelayResult``.

Atomicity uses ``Path.replace`` after writing to a sibling ``.tmp`` file
(POSIX atomic, NTFS-atomic for non-shared opens). Networked-FS event
mechanisms like ``inotify`` are unreliable cross-node, so the design is
poll-based.

Whitelist
---------

Auto-approved (no consent prompt):

- ``git_pull``    -> ``git pull --ff-only``
- ``git_push``    -> ``git push [<refspec>]``
- ``git_fetch``   -> ``git fetch --all --prune``
- ``git_status``  -> ``git status --porcelain=v2``
- ``git_rebase``  -> ``git pull --rebase``  (recovers from no-conflict divergence; bails on conflict)

Consent-required (requires explicit ``relay-approve <uuid>`` from the user):

- ``wandb_sync``  -> ``wandb sync --sync-all``

See ``electricrag/dev/README.md`` for the full setup and protocol.

Usage
-----

Client (notebook cell on compute node)::

    from electricrag.dev.relay import request
    result = request("git_pull")
    print(result.stdout)

Daemon (login node, run once under tmux)::

    tmux new -d -s relay 'cd ~/electricrag && python -m electricrag.dev.relay daemon'
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aexp.utils.atomic import atomic_write, doc_op_with_retry

log = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

DEFAULT_QUEUE = Path.home() / ".relay"

HEARTBEAT_INTERVAL_S = 5.0          # daemon writes heartbeat every N seconds
HEARTBEAT_MAX_AGE_S = 30.0          # client treats older heartbeat as down
GC_INTERVAL_S = 60.0                # daemon GC sweep cadence
GC_MAX_AGE_S = 7 * 24 * 3600        # purge outbox/log/approved/rejected older than this
PENDING_TTL_S = 24 * 3600           # daemon-side timeout for un-decided consent

DAEMON_POLL_INTERVAL_S = 0.5
CLIENT_POLL_INTERVAL_S = 0.25

DEFAULT_CLIENT_TIMEOUT_S = 60.0          # auto-approved ops
DEFAULT_CONSENT_TIMEOUT_S = 600.0        # consent-required ops (10 min)

MAX_ARG_LENGTH = 256
MAX_ARGS = 32

QUEUE_SUBDIRS = (
    "inbox", "pending", "processing", "outbox", "log",
    "stale", "approved", "rejected", "_bin",
)

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
        If True, requires the user to touch ``approved/<uuid>`` before
        execution.
    args_regex : str or None
        If set, every per-request arg must ``re.fullmatch`` this regex.
        If None, no per-request args are accepted (the request's
        ``args`` field must be empty).
    """

    argv: list[str]
    consent: bool
    args_regex: Optional[str] = None


ALLOWED: dict[str, OpSpec] = {
    # Auto-approved (auditable + reversible by their nature)
    "git_pull":   OpSpec(["git", "pull", "--ff-only"], consent=False),
    "git_push":   OpSpec(["git", "push"], consent=False, args_regex=r"^[a-zA-Z0-9._/\-]+$"),
    "git_fetch":  OpSpec(["git", "fetch", "--all", "--prune"], consent=False),
    "git_status": OpSpec(["git", "status", "--porcelain=v2"], consent=False),
    "git_rebase": OpSpec(["git", "pull", "--rebase"], consent=False),
    # Consent-required (requires explicit relay-approve; no other gating)
    "wandb_sync": OpSpec(["wandb", "sync", "--sync-all"], consent=True),
}

# Cwd allowlist. Default: empty tuple = "any subdir under $HOME is allowed"
# (still enforces the under-$HOME check for security). Project-specific
# lockdowns set the env var AEXP_RELAY_CWD_NAMES to a comma-separated
# list of top-level dir names under $HOME (e.g. "myrepo,other-repo").
def _read_cwd_allowlist() -> tuple[str, ...]:
    raw = os.environ.get("AEXP_RELAY_CWD_NAMES", "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())

_ALLOWED_CWD_NAMES = _read_cwd_allowlist()


# ============================================================================
# Errors
# ============================================================================


class RelayError(RuntimeError):
    """Base for all relay-protocol errors raised by the client."""


class RelayDownError(RelayError):
    """Heartbeat is missing or older than ``HEARTBEAT_MAX_AGE_S``."""


class RelayValidationError(RelayError):
    """Daemon rejected a request as invalid (whitelist, regex, args)."""


class RelayRejectedError(RelayError):
    """Kaden touched ``rejected/<uuid>`` for a consent-required request."""


class RelayTimeoutError(RelayError):
    """Client's per-call timeout elapsed before a response arrived."""


class RelayCrashedError(RelayError):
    """Daemon died mid-execution; outbox was synthesized on next start."""


# ============================================================================
# Result type
# ============================================================================


@dataclass
class RelayResult:
    """Return value of a successful (or completed-with-nonzero-rc) request.

    ``returncode`` is the subprocess exit code; non-zero is *not* an
    exception (the daemon ran the command and got a result; the client
    surfaces the result as-is). Protocol-level failures (down,
    rejected, timeout, etc.) raise ``RelayError`` subclasses instead of
    returning a ``RelayResult``.
    """

    request_id: str
    op: str
    returncode: int
    stdout: str
    duration_s: float


# ============================================================================
# Queue layout
# ============================================================================


def ensure_queue(queue: Path) -> None:
    """Create the queue directory and all subdirs if missing.

    Mode is left at the umask default; the daemon's startup will tighten
    perms on its own to ``0o700``.
    """
    queue.mkdir(parents=True, exist_ok=True)
    for sub in QUEUE_SUBDIRS:
        (queue / sub).mkdir(parents=True, exist_ok=True)


def _request_paths(queue: Path, request_id: str) -> dict[str, Path]:
    """Return the standard set of per-request paths."""
    return {
        "inbox":      queue / "inbox" / f"{request_id}.json",
        "processing": queue / "processing" / f"{request_id}.json",
        "pending":    queue / "pending" / f"{request_id}.json",
        "outbox":     queue / "outbox" / f"{request_id}.json",
        "log":        queue / "log" / f"{request_id}.txt",
        "stale":      queue / "stale" / f"{request_id}.json",
        "approved":   queue / "approved" / request_id,
        "rejected":   queue / "rejected" / request_id,
    }


def _resolve_cwd(cwd_str: str) -> Path:
    """Expand and resolve a cwd string, then verify it's allowlisted.

    Raises RelayValidationError if the resolved cwd is not under
    ``Path.home()`` or its top-level name doesn't match the allowlist.
    """
    cwd = Path(cwd_str).expanduser().resolve()
    home = Path.home().resolve()
    try:
        rel = cwd.relative_to(home)
    except ValueError:
        raise RelayValidationError(
            f"cwd not under home: {cwd} (home={home})"
        )
    # If a name allowlist is set, the cwd's first segment must match.
    # Empty allowlist = no name restriction beyond the under-$HOME check.
    if _ALLOWED_CWD_NAMES and (not rel.parts or rel.parts[0] not in _ALLOWED_CWD_NAMES):
        raise RelayValidationError(
            f"cwd not in allowlist: {cwd} (allowed names under home: {_ALLOWED_CWD_NAMES})"
        )
    return cwd


# ============================================================================
# Validation
# ============================================================================


def validate_request(payload: dict) -> tuple[str, list[str], Path]:
    """Validate a request payload and return ``(op, args, cwd)``.

    Parameters
    ----------
    payload : dict
        Parsed JSON from an inbox file.

    Returns
    -------
    op : str
        The whitelisted operation name.
    args : list of str
        Validated per-request arguments.
    cwd : Path
        Resolved, allowlisted working directory.

    Raises
    ------
    RelayValidationError
        If any field is missing, malformed, exceeds limits, or fails
        the per-op regex.
    """
    op = payload.get("op")
    if not isinstance(op, str) or op not in ALLOWED:
        raise RelayValidationError(
            f"unknown op: {op!r}; allowed: {sorted(ALLOWED)}"
        )

    args = payload.get("args", [])
    if not isinstance(args, list):
        raise RelayValidationError(
            f"args must be a list, got {type(args).__name__}"
        )
    if len(args) > MAX_ARGS:
        raise RelayValidationError(
            f"too many args ({len(args)} > {MAX_ARGS})"
        )

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

    cwd_str = payload.get("cwd")
    if not cwd_str:
        raise RelayValidationError(
            "cwd is required (the client should pass an explicit cwd; "
            "RelayClient defaults to Path.cwd())"
        )
    if not isinstance(cwd_str, str):
        raise RelayValidationError(
            f"cwd must be str, got {type(cwd_str).__name__}"
        )
    cwd = _resolve_cwd(cwd_str)

    return op, args, cwd


# ============================================================================
# JSON helpers
# ============================================================================


def _read_json(path: Path) -> dict:
    """Read a JSON file; return ``{}`` on missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: dict) -> Path:
    """Atomic JSON write."""
    return atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))


# ============================================================================
# Daemon
# ============================================================================


@dataclass
class Daemon:
    """The relay daemon that runs on the login node.

    Designed for testability: ``_tick()`` runs one iteration of the main
    loop and returns; ``run()`` calls ``_tick()`` in a sleep loop.
    Tests instantiate ``Daemon(queue=tmp_path)`` and drive ``_tick()``
    directly.
    """

    queue: Path
    _last_heartbeat: float = 0.0
    _last_gc: float = 0.0
    _shutdown: bool = False
    # Tracks pending requests' first-seen-time (for client-visible
    # pending detection) -- not authoritative for the TTL, that comes
    # from the file's mtime.
    _pending_seen: dict[str, float] = field(default_factory=dict)

    # ---- lifecycle ----

    def startup(self) -> None:
        """One-time startup: ensure queue, recover stale processing,
        write PID file, atomicity self-test."""
        ensure_queue(self.queue)
        try:
            self.queue.chmod(0o700)
        except OSError:  # pragma: no cover — Windows perm semantics differ
            pass
        self._atomicity_self_test()
        self._recover_stale_processing()
        self._write_pid()
        self._touch_heartbeat()
        log.info("relay daemon started; queue=%s pid=%d", self.queue, os.getpid())

    def run(self) -> None:
        """Main loop. Returns when ``_shutdown`` is set (e.g., SIGTERM)."""
        self.startup()
        try:
            while not self._shutdown:
                self._tick()
                time.sleep(DAEMON_POLL_INTERVAL_S)
        finally:
            log.info("relay daemon stopping; pid=%d", os.getpid())

    def stop(self) -> None:
        """Signal the main loop to exit on its next iteration."""
        self._shutdown = True

    # ---- one iteration ----

    def _tick(self) -> None:
        """One iteration of the main loop. Idempotent; safe to call in tests."""
        now = time.monotonic()
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL_S:
            self._touch_heartbeat()
            self._last_heartbeat = now
        if now - self._last_gc >= GC_INTERVAL_S:
            self._gc_old_files()
            self._last_gc = now

        # Process new inbox requests in mtime order.
        inbox = self.queue / "inbox"
        for req_path in sorted(inbox.glob("*.json"), key=lambda p: p.stat().st_mtime):
            self._handle_inbox(req_path)

        # Check pending requests for consent / timeout.
        pending = self.queue / "pending"
        for req_path in list(pending.glob("*.json")):
            self._check_consent(req_path)

    # ---- inbox handling ----

    def _handle_inbox(self, req_path: Path) -> None:
        """Validate one inbox request and route it.

        Auto-approved -> processing/ -> execute.
        Consent-required -> pending/ + log to consent.log.
        Validation failure -> outbox/ with error.
        """
        request_id = req_path.stem
        paths = _request_paths(self.queue, request_id)
        payload = _read_json(req_path)
        if not payload:
            self._finalize_error(
                request_id, op="?", error_kind="validation",
                message=f"inbox file empty or unparseable: {req_path.name}",
            )
            try:
                req_path.unlink()
            except FileNotFoundError:
                pass
            return

        try:
            op, args, cwd = validate_request(payload)
        except RelayValidationError as exc:
            self._finalize_error(
                request_id, op=str(payload.get("op", "?")),
                error_kind="validation", message=str(exc),
            )
            try:
                req_path.unlink()
            except FileNotFoundError:
                pass
            return

        spec = ALLOWED[op]
        if spec.consent:
            # Move to pending/. Keep the validated payload so we don't
            # re-validate at consent-check time.
            payload_out = {"op": op, "args": args, "cwd": str(cwd),
                           "submitted_at": payload.get("submitted_at")}
            _write_json(paths["pending"], payload_out)
            try:
                req_path.unlink()
            except FileNotFoundError:
                pass
            self._log_consent_request(request_id, op, args, cwd)
            log.info("[%s] pending consent: %s %s", request_id, op, args)
        else:
            # Move to processing/ atomically before executing.
            payload_out = {"op": op, "args": args, "cwd": str(cwd)}
            _write_json(paths["processing"], payload_out)
            try:
                req_path.unlink()
            except FileNotFoundError:
                pass
            self._execute(request_id, op, args, cwd)

    # ---- consent handling ----

    def _check_consent(self, req_path: Path) -> None:
        """Check if a pending request has been approved, rejected, or timed out."""
        request_id = req_path.stem
        paths = _request_paths(self.queue, request_id)
        # Rejected wins (fail-safe).
        if paths["rejected"].exists():
            self._finalize_error(
                request_id, op=str(_read_json(req_path).get("op", "?")),
                error_kind="rejected", message="user rejected request",
            )
            self._cleanup_consent_markers(request_id)
            try:
                req_path.unlink()
            except FileNotFoundError:
                pass
            return
        if paths["approved"].exists():
            payload = _read_json(req_path)
            op = payload.get("op", "?")
            args = payload.get("args", [])
            cwd_str = payload.get("cwd")
            if not (op in ALLOWED and isinstance(args, list) and isinstance(cwd_str, str)):
                # Defensive — the pending payload was already validated
                # at inbox time, so this shouldn't happen.
                self._finalize_error(
                    request_id, op=str(op), error_kind="validation",
                    message="pending payload corrupt",
                )
            else:
                cwd = Path(cwd_str)
                _write_json(paths["processing"], payload)
                self._execute(request_id, op, args, cwd)
            self._cleanup_consent_markers(request_id)
            try:
                req_path.unlink()
            except FileNotFoundError:
                pass
            return
        # Timeout check.
        try:
            mtime = req_path.stat().st_mtime
        except FileNotFoundError:
            return
        if time.time() - mtime > PENDING_TTL_S:
            self._finalize_error(
                request_id, op=str(_read_json(req_path).get("op", "?")),
                error_kind="timeout",
                message=f"consent not granted within {PENDING_TTL_S}s",
            )
            self._cleanup_consent_markers(request_id)
            try:
                req_path.unlink()
            except FileNotFoundError:
                pass

    def _cleanup_consent_markers(self, request_id: str) -> None:
        paths = _request_paths(self.queue, request_id)
        for marker in (paths["approved"], paths["rejected"]):
            try:
                marker.unlink()
            except FileNotFoundError:
                pass

    def _log_consent_request(self, request_id: str, op: str, args: list[str], cwd: Path) -> None:
        """Append one line to ``consent.log`` so a side tmux pane can ``tail -f``."""
        line = "{ts} [{rid}] op={op} args={args} cwd={cwd}\n".format(
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            rid=request_id, op=op, args=args, cwd=cwd,
        )
        with (self.queue / "consent.log").open("a", encoding="utf-8") as f:
            f.write(line)

    # ---- execution ----

    def _execute(self, request_id: str, op: str, args: list[str], cwd: Path) -> None:
        """Run the whitelisted command, stream output to log, write outbox."""
        spec = ALLOWED[op]
        cmd = list(spec.argv) + list(args)
        paths = _request_paths(self.queue, request_id)
        log_path = paths["log"]

        log.info("[%s] exec: %s (cwd=%s)", request_id, " ".join(cmd), cwd)
        t_start = time.monotonic()
        rc = -1
        try:
            with log_path.open("w", encoding="utf-8") as logf:
                logf.write(f"$ {' '.join(cmd)}  (cwd={cwd})\n")
                logf.flush()
                try:
                    proc = subprocess.Popen(
                        cmd, cwd=str(cwd),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        bufsize=1, text=True,
                    )
                except (FileNotFoundError, OSError) as exc:
                    logf.write(f"\n[relay] failed to spawn: {exc}\n")
                    rc = 127
                else:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        logf.write(line)
                        logf.flush()
                    rc = proc.wait()
        except Exception as exc:  # pragma: no cover — defensive
            log.exception("[%s] unexpected exec error", request_id)
            try:
                with log_path.open("a", encoding="utf-8") as logf:
                    logf.write(f"\n[relay] internal error: {exc!r}\n")
            except OSError:
                pass
            rc = -1
        duration = time.monotonic() - t_start

        # Move from processing -> done (delete the processing marker).
        try:
            paths["processing"].unlink()
        except FileNotFoundError:
            pass

        # Read full log into outbox payload (small for git ops; bounded
        # by command output size for wandb_sync).
        try:
            stdout = log_path.read_text(encoding="utf-8")
        except OSError:
            stdout = ""

        _write_json(paths["outbox"], {
            "request_id": request_id,
            "op": op,
            "status": "completed",
            "returncode": rc,
            "stdout": stdout,
            "duration_s": round(duration, 4),
        })
        log.info("[%s] done rc=%d duration=%.2fs", request_id, rc, duration)

    # ---- finalize errors ----

    def _finalize_error(
        self, request_id: str, *, op: str, error_kind: str, message: str,
    ) -> None:
        """Write an outbox payload for a non-execution failure path."""
        paths = _request_paths(self.queue, request_id)
        _write_json(paths["outbox"], {
            "request_id": request_id,
            "op": op,
            "status": error_kind,
            "returncode": -1,
            "stdout": "",
            "error": message,
            "duration_s": 0.0,
        })
        log.info("[%s] %s: %s", request_id, error_kind, message)

    # ---- heartbeat / pid / GC / recovery ----

    def _touch_heartbeat(self) -> None:
        _write_json(self.queue / "heartbeat", {
            "ts_monotonic": time.monotonic(),
            "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
        })

    def _write_pid(self) -> None:
        _write_json(self.queue / "pid", {
            "pid": os.getpid(),
            "started_wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    def _gc_old_files(self) -> None:
        """Remove old outbox / log / approved / rejected files."""
        now = time.time()
        for sub in ("outbox", "log", "approved", "rejected", "stale"):
            d = self.queue / sub
            if not d.exists():
                continue
            for p in d.iterdir():
                try:
                    if now - p.stat().st_mtime > GC_MAX_AGE_S:
                        p.unlink()
                except (FileNotFoundError, OSError):
                    pass

    def _recover_stale_processing(self) -> None:
        """On startup, treat anything in processing/ as a prior crash."""
        proc_dir = self.queue / "processing"
        if not proc_dir.exists():
            return
        for p in list(proc_dir.glob("*.json")):
            request_id = p.stem
            payload = _read_json(p)
            paths = _request_paths(self.queue, request_id)
            try:
                p.replace(paths["stale"])
            except OSError:
                # If we can't move it, at least synthesize the response.
                pass
            self._finalize_error(
                request_id,
                op=str(payload.get("op", "?")),
                error_kind="crashed",
                message="daemon died mid-execution; please retry",
            )
            log.warning("[%s] recovered stale processing entry", request_id)

    def _atomicity_self_test(self) -> None:
        """Verify atomic_write works on the queue's filesystem.

        Networked filesystems vary; this is a cheap canary at startup
        rather than discovering the failure mid-request.
        """
        probe = self.queue / "_probe.json"
        try:
            atomic_write(probe, json.dumps({"ok": True}))
            data = json.loads(probe.read_text(encoding="utf-8"))
            if not data.get("ok"):
                raise RuntimeError("probe content mismatch")
            probe.unlink()
        except Exception as exc:
            raise RuntimeError(
                f"atomicity self-test failed at {probe}: {exc!r}. "
                "Confirm the filesystem supports POSIX rename semantics."
            ) from exc


# ============================================================================
# Client
# ============================================================================


def _check_heartbeat(queue: Path) -> None:
    """Raise RelayDownError if heartbeat is missing or stale."""
    hb_path = queue / "heartbeat"
    try:
        mtime = hb_path.stat().st_mtime
    except FileNotFoundError:
        raise RelayDownError(
            f"heartbeat file not found at {hb_path}. "
            "Daemon not running? Bootstrap on the login node:\n"
            "  tmux new -d -s relay 'python -m aexp airgapped daemon'"
        )
    age = time.time() - mtime
    if age > HEARTBEAT_MAX_AGE_S:
        raise RelayDownError(
            f"heartbeat stale ({age:.0f}s old at {hb_path}). "
            "Daemon may have died. Re-bootstrap on the login node:\n"
            "  tmux kill-session -t relay 2>/dev/null; "
            "tmux new -d -s relay 'python -m aexp airgapped daemon'"
        )


def request(
    op: str,
    args: Optional[list[str]] = None,
    *,
    queue: Optional[Path] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
    poll_interval: float = CLIENT_POLL_INTERVAL_S,
) -> RelayResult:
    """Submit a request to the daemon and block until completion.

    Parameters
    ----------
    op : str
        One of the keys in ``ALLOWED``.
    args : list of str, optional
        Per-request arguments. Required for ops whose ``args_regex`` is
        set; must be empty/None otherwise.
    queue : Path, optional
        Queue directory. Defaults to ``~/.relay``.
    cwd : str, optional
        Working directory for the daemon to ``cd`` into. Must resolve
        under ``$HOME`` (and, if ``AEXP_RELAY_CWD_NAMES`` is set, the
        first segment must match the allowlist). Default is ``Path.cwd()``.
    timeout : float, optional
        Client-side timeout in seconds. Default is 60s for auto-approved
        ops, 600s for consent-required ops.
    poll_interval : float, optional
        How often to poll the outbox.

    Returns
    -------
    RelayResult
        Subprocess result. Non-zero ``returncode`` is *not* an exception.

    Raises
    ------
    RelayDownError, RelayValidationError, RelayRejectedError,
    RelayTimeoutError, RelayCrashedError
    """
    queue = (queue or DEFAULT_QUEUE).expanduser()
    ensure_queue(queue)
    _check_heartbeat(queue)

    if op not in ALLOWED:
        raise RelayValidationError(
            f"unknown op: {op!r}; allowed: {sorted(ALLOWED)}"
        )
    spec = ALLOWED[op]
    if timeout is None:
        timeout = DEFAULT_CONSENT_TIMEOUT_S if spec.consent else DEFAULT_CLIENT_TIMEOUT_S

    request_id = uuid.uuid4().hex
    payload = {
        "op": op,
        "args": list(args or []),
        "cwd": cwd or str(Path.cwd()),
        "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    paths = _request_paths(queue, request_id)
    _write_json(paths["inbox"], payload)
    log.debug("submitted request %s (op=%s, args=%s)", request_id, op, args)

    deadline = time.monotonic() + timeout
    consent_announced = False
    while True:
        if paths["outbox"].exists():
            response = _read_json(paths["outbox"])
            return _interpret_response(response, request_id, op)
        # Surface consent prompt the first time we see the request in pending.
        if (
            spec.consent
            and not consent_announced
            and paths["pending"].exists()
        ):
            print(
                f"[relay] Awaiting consent for {op} (request {request_id}).\n"
                f"        Approve: bash ~/.relay/_bin/relay-approve {request_id}\n"
                f"        Reject:  bash ~/.relay/_bin/relay-reject  {request_id}",
                file=sys.stderr,
                flush=True,
            )
            consent_announced = True
        if time.monotonic() >= deadline:
            raise RelayTimeoutError(
                f"no response within {timeout:.0f}s for request {request_id} "
                f"(op={op}). The daemon may still complete it; check "
                f"{paths['outbox']} later."
            )
        time.sleep(poll_interval)


def _interpret_response(response: dict, request_id: str, op: str) -> RelayResult:
    """Map a daemon outbox payload to a RelayResult or raise."""
    status = response.get("status", "?")
    if status == "completed":
        return RelayResult(
            request_id=request_id,
            op=op,
            returncode=int(response.get("returncode", -1)),
            stdout=str(response.get("stdout", "")),
            duration_s=float(response.get("duration_s", 0.0)),
        )
    msg = str(response.get("error", "(no message)"))
    if status == "validation":
        raise RelayValidationError(f"daemon rejected {op}: {msg}")
    if status == "rejected":
        raise RelayRejectedError(f"user rejected {op} (request {request_id})")
    if status == "timeout":
        raise RelayTimeoutError(f"daemon-side consent timeout for {op}: {msg}")
    if status == "crashed":
        raise RelayCrashedError(f"{op}: {msg}")
    raise RelayError(f"unknown response status {status!r} for {op}: {response!r}")


# ============================================================================
# CLI
# ============================================================================


def _cli_daemon(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.log:
        # Add a file handler in addition to the stderr stream handler.
        # Ensure the log file's parent exists — the daemon's startup() creates
        # the queue dir, but the log handler opens its file first. Without
        # this, ``--log ~/.relay/daemon.log`` fails on a fresh machine where
        # ~/.relay/ doesn't exist yet (caught by the laptop daemon smoke).
        log_path = Path(args.log).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
    daemon = Daemon(queue=Path(args.queue).expanduser())
    daemon.run()
    return 0


def _cli_install_helpers(args: argparse.Namespace) -> int:
    queue = Path(args.queue).expanduser()
    ensure_queue(queue)
    bin_dir = queue / "_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    helpers = {
        "relay-approve": '#!/usr/bin/env bash\nset -eu\ntouch "$HOME/.relay/approved/$1"\necho "approved: $1"\n',
        "relay-reject":  '#!/usr/bin/env bash\nset -eu\ntouch "$HOME/.relay/rejected/$1"\necho "rejected: $1"\n',
        "relay-list-pending": (
            '#!/usr/bin/env bash\nset -eu\nls -t "$HOME/.relay/pending/" 2>/dev/null '
            "| sed 's/\\.json$//'\n"
        ),
    }
    for name, body in helpers.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        try:
            path.chmod(0o755)
        except OSError:  # pragma: no cover — Windows
            pass
    print(f"installed helpers to {bin_dir}")
    return 0


def _cli_status(args: argparse.Namespace) -> int:
    queue = Path(args.queue).expanduser()
    hb = queue / "heartbeat"
    if not hb.exists():
        print(f"heartbeat: MISSING ({hb})")
        return 1
    age = time.time() - hb.stat().st_mtime
    state = "fresh" if age <= HEARTBEAT_MAX_AGE_S else "STALE"
    payload = _read_json(hb)
    print(f"heartbeat: {state} ({age:.1f}s old, pid={payload.get('pid')})")
    pending_count = len(list((queue / "pending").glob("*.json"))) if (queue / "pending").exists() else 0
    inbox_count = len(list((queue / "inbox").glob("*.json"))) if (queue / "inbox").exists() else 0
    processing_count = len(list((queue / "processing").glob("*.json"))) if (queue / "processing").exists() else 0
    print(f"inbox: {inbox_count}  processing: {processing_count}  pending: {pending_count}")
    return 0 if state == "fresh" else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="electricrag.dev.relay")
    # Shared --queue arg: defined on a parent parser so each subcommand
    # accepts it AFTER the subcommand name (the natural CLI shape).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--queue", default=str(DEFAULT_QUEUE),
        help="Queue directory (default: ~/.relay)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_daemon = sub.add_parser(
        "daemon", parents=[common],
        help="Run the relay daemon (login node)",
    )
    p_daemon.add_argument("--log", default=None, help="Optional log file path")
    p_daemon.set_defaults(func=_cli_daemon)

    p_install = sub.add_parser(
        "install-helpers", parents=[common],
        help="Install relay-approve / relay-reject / relay-list-pending shell helpers",
    )
    p_install.set_defaults(func=_cli_install_helpers)

    p_status = sub.add_parser(
        "status", parents=[common],
        help="Print daemon liveness summary",
    )
    p_status.set_defaults(func=_cli_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
