"""High-level client API for the airgapped relay.

This module wraps :func:`aexp.airgapped._relay.request` with semantic helper
methods so callers don't have to remember the op-name -> command mapping or
hand-construct ``args``. It also designs out two arg-ordering frictions:

- **F7** -- ``request("git_push")`` raises because the whitelist requires at
  least one arg. The wrapper builds ``args`` correctly.
- **F8** -- ``request("git_push", args=["main"])`` is interpreted as
  ``git push main`` where ``main`` is treated as a *remote* name. The
  wrapper takes a ``branch`` + ``remote`` and passes them in the right order.

The relay runs whitelisted git/wandb commands on an internet-having
sibling node (login node on an HPC, jumpbox elsewhere) over SSH, on
behalf of an agent whose compute machine is network-isolated. The
client (and agent) run on the user's local machine (where Claude Code runs).

Recommended usage::

    from aexp.airgapped import RelayClient
    relay = RelayClient(ssh_host="cluster-login", remote_repo="~/my-project")
    r = relay.pull()
    print(r.returncode, r.stdout)

``ssh_host`` and ``remote_repo`` may also come from the environment
(``AEXP_RELAY_SSH_HOST`` / ``AEXP_RELAY_REMOTE_REPO``); when both are set
``RelayClient()`` needs no arguments.

For consent-required ops (``wandb_sync``), use :meth:`RelayClient.request`
with ``approve=True``.
"""
from __future__ import annotations

from pathlib import Path

from aexp.airgapped._relay import (
    DEFAULT_CLIENT_TIMEOUT_S,
    DEFAULT_CONNECT_TIMEOUT_S,
    RelayResult,
    request,
)


class RelayClient:
    """Higher-level client for the airgapped relay.

    Holds default ``ssh_host``, ``remote_repo``, timeouts, and audit-log
    path so individual calls don't have to specify them. Method names
    mirror common git verbs plus a generic :meth:`request` for the
    whitelist's other ops.

    Parameters
    ----------
    ssh_host : str, optional
        SSH host the relay connects to -- ideally an ``~/.ssh/config``
        Host alias, so auth/keys/MFA live in your SSH config. Falls back
        to ``$AEXP_RELAY_SSH_HOST``.
    remote_repo : str, optional
        Absolute path of the git repo on the login node. Falls back to
        ``$AEXP_RELAY_REMOTE_REPO``.
    default_timeout : float, optional
        Default per-call timeout (seconds) for auto-approved ops.
        Defaults to 60s. Consent-required ops have their own longer
        default (10 min) inside :func:`request`.
    connect_timeout : float, optional
        ``ssh -o ConnectTimeout`` value. Defaults to 10s.
    audit_log : Path or str, optional
        Where each relay op appends a one-line audit record. Falls back
        to ``$AEXP_RELAY_AUDIT_LOG`` then ``~/.aexp/airgapped-relay.log``.
    """

    def __init__(
        self,
        *,
        ssh_host: str | None = None,
        remote_repo: str | None = None,
        default_timeout: float = DEFAULT_CLIENT_TIMEOUT_S,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
        audit_log: Path | str | None = None,
    ) -> None:
        self.ssh_host: str | None = ssh_host
        self.remote_repo: str | None = remote_repo
        self.default_timeout: float = default_timeout
        self.connect_timeout: float = connect_timeout
        self.audit_log: Path | str | None = audit_log

    # ------------------------------------------------------------------
    # Git ops (designed out F7/F8)
    # ------------------------------------------------------------------

    def pull(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git pull --ff-only`` on the login node.

        Returns the :class:`RelayResult`. A non-zero ``returncode`` is not
        an exception (the relay ran git and got a result); inspect
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
        """Run ``git push <remote> <branch>`` on the login node.

        Defaults to ``git push origin HEAD`` -- pushes the currently
        checked-out branch to the matching upstream. Override either
        parameter for explicit pushes.
        """
        return self._call("git_push", args=[remote, branch], timeout=timeout)

    def fetch(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git fetch --all --prune`` on the login node."""
        return self._call("git_fetch", timeout=timeout)

    def status(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git status --porcelain=v2`` on the login node's repo."""
        return self._call("git_status", timeout=timeout)

    def rebase(self, *, timeout: float | None = None) -> RelayResult:
        """Run ``git pull --rebase`` on the login node.

        Useful when the local branch has diverged from the remote without
        conflicts. On real conflicts the rebase aborts and ``r.stdout``
        carries the standard git conflict message; ``returncode`` is
        non-zero.
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
        approve: bool = False,
        timeout: float | None = None,
    ) -> RelayResult:
        """Send an arbitrary whitelisted op (escape hatch for non-git ops).

        Use this for ops not exposed as dedicated methods -- e.g. the
        consent-required ``wandb_sync``, which requires ``approve=True``.
        Confirm with the user before passing ``approve=True``; it is an
        outward-facing operation.
        """
        return self._call(op, args=args, approve=approve, timeout=timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call(
        self,
        op: str,
        *,
        args: list[str] | None = None,
        approve: bool = False,
        timeout: float | None = None,
    ) -> RelayResult:
        effective_timeout = timeout if timeout is not None else self.default_timeout
        return request(
            op,
            args=args,
            ssh_host=self.ssh_host,
            remote_repo=self.remote_repo,
            approve=approve,
            timeout=effective_timeout,
            connect_timeout=self.connect_timeout,
            audit_log=self.audit_log,
        )
