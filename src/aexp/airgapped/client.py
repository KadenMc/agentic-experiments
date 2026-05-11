"""High-level client API for the airgapped relay.

This module wraps :func:`aexp.airgapped._relay.request` with semantic helper
methods so callers don't have to hand-construct args / remember the
op-name → command mapping. It also designs out two frictions documented
in the electricrag 2026-05-10 session (see ``aexp.md`` Surface 5):

- **F7** — ``request("git_push")`` raises because the daemon's whitelist
  requires at least one arg. The wrapper builds ``args`` correctly.
- **F8** — ``request("git_push", args=["main"])`` is interpreted as
  ``git push main`` where ``main`` is treated as a remote name. The
  wrapper takes a ``branch`` + ``remote`` and passes them in the right
  order.

Recommended usage from a sandbox notebook cell::

    from aexp.airgapped import RelayClient
    relay = RelayClient()  # uses Path.cwd() as cwd, ~/.relay as queue
    r = relay.pull()
    print(r.returncode, r.stdout)

For ops not covered by a dedicated method (e.g. consent-required ops
like ``wandb_sync``), use the lower-level escape hatch::

    r = relay.request("wandb_sync", timeout=600)

Or import :func:`aexp.airgapped.request` directly for fully-manual control.
"""
from __future__ import annotations

from pathlib import Path

from aexp.airgapped._relay import (
    DEFAULT_CLIENT_TIMEOUT_S,
    DEFAULT_QUEUE,
    RelayResult,
    request,
)


class RelayClient:
    """Higher-level client for the airgapped relay.

    Holds default ``queue``, ``cwd``, and ``default_timeout`` so individual
    calls don't have to specify them. Method names mirror common git verbs
    plus a generic :meth:`request` for the whitelist's other ops.

    Parameters
    ----------
    queue : Path or str, optional
        Queue directory. Defaults to ``~/.relay`` (matches the daemon's
        default; override only if you ran the daemon with a custom
        ``--queue``).
    cwd : Path or str, optional
        Working directory the daemon will ``cd`` into before running each
        command. Defaults to ``Path.cwd()`` at construction time. Must
        resolve under ``$HOME`` and (if ``AEXP_RELAY_CWD_NAMES`` is set)
        match the allowlist.
    default_timeout : float, optional
        Default per-call timeout in seconds for auto-approved ops.
        Defaults to ``DEFAULT_CLIENT_TIMEOUT_S`` (60s). Consent-required
        ops have their own longer default (10 min) inside :func:`request`.
    """

    def __init__(
        self,
        *,
        queue: Path | str | None = None,
        cwd: Path | str | None = None,
        default_timeout: float = DEFAULT_CLIENT_TIMEOUT_S,
    ) -> None:
        self.queue: Path = Path(queue).expanduser() if queue else DEFAULT_QUEUE
        self.cwd: Path = Path(cwd).expanduser() if cwd else Path.cwd()
        self.default_timeout: float = default_timeout

    # ------------------------------------------------------------------
    # Git ops (designed out F7/F8)
    # ------------------------------------------------------------------

    def pull(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git pull --ff-only`` on the daemon side.

        Returns the :class:`RelayResult`. Non-zero ``returncode`` is not
        an exception (the daemon ran git and got a result); inspect
        ``r.stdout`` for what git said.
        """
        return self._call("git_pull", timeout=timeout)

    def push(
        self,
        *,
        branch: str = "HEAD",
        remote: str = "origin",
        timeout: float | None = None,
    ) -> RelayResult:
        """Run ``git push <remote> <branch>`` on the daemon side.

        Defaults to ``git push origin HEAD`` which pushes the currently
        checked-out branch to the matching upstream — friendly for the
        common case. Override either parameter for explicit pushes.
        """
        return self._call("git_push", args=[remote, branch], timeout=timeout)

    def fetch(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git fetch --all --prune`` on the daemon side."""
        return self._call("git_fetch", timeout=timeout)

    def status(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git status --porcelain=v2`` on the daemon side."""
        return self._call("git_status", timeout=timeout)

    def rebase(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git pull --rebase`` on the daemon side.

        Useful when the local branch has diverged from the remote without
        conflicts; the daemon will rebase and emit clean output. On real
        conflicts the rebase aborts and ``r.stdout`` carries the standard
        git conflict message; ``returncode`` will be non-zero.
        """
        return self._call("git_rebase", timeout=timeout)

    # ------------------------------------------------------------------
    # Generic escape hatch
    # ------------------------------------------------------------------

    def request(
        self,
        op: str,
        *,
        args: list[str] | None = None,
        timeout: float | None = None,
    ) -> RelayResult:
        """Send an arbitrary whitelisted op (escape hatch for non-git ops).

        Use this for ops not exposed as dedicated methods — e.g. consent-
        required ones like ``wandb_sync``. For consent-required ops, pass
        ``timeout`` high enough to allow user interaction (default 10 min
        is set inside :func:`request` if the op is consent-required).
        """
        return self._call(op, args=args, timeout=timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call(
        self,
        op: str,
        *,
        args: list[str] | None = None,
        timeout: float | None = None,
    ) -> RelayResult:
        effective_timeout = timeout if timeout is not None else self.default_timeout
        return request(
            op,
            args=args,
            queue=self.queue,
            cwd=str(self.cwd),
            timeout=effective_timeout,
        )
