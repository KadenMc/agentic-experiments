"""Airgapped-compute support: direct-SSH bridge to a sibling node.

Designed for environments where the agent's runtime is network-isolated
(common at secure HPC sites, also at regulated/clinical setups and some
government/research labs) but a sibling node (login node, jumpbox) has
internet. The relay lets the agent run whitelisted commands (git
operations, a consent-gated ``wandb sync``) on that sibling node by
SSHing to it from the user's local machine (where Claude Code runs).

Public surface:

- :class:`RelayClient` -- high-level client; recommended for most callers.
  Wraps the low-level :func:`request` with semantic methods
  (``pull``, ``push``, ``fetch``, ``status``, ``rebase``).
- :func:`request` -- low-level escape hatch for callers who want full
  control over per-call host / repo / args.
- :func:`check_connection` -- verify the login node is reachable.
- :func:`validate_request` -- whitelist + arg validation (exposed for
  tests and callers building requests by hand).
- :class:`RelayResult`, :class:`RelayError` (+ subclasses) -- result and
  error types.

This subpackage is **not** imported at ``aexp`` package init -- users
opt in by importing ``aexp.airgapped``. The relay is genuinely
infrastructure-specific (most users don't have airgapped-compute
constraints), so default-installing it would be bloat.

See ``docs/airgapped.md`` for the SSH setup recipe (``~/.ssh/config``
host alias, ControlMaster for MFA reuse) and the full protocol.
"""
from __future__ import annotations

from aexp.airgapped._relay import (
    # Whitelist + spec
    ALLOWED,
    OpSpec,
    # Result + errors
    RelayDownError,
    RelayError,
    RelayRejectedError,
    RelayResult,
    RelayTimeoutError,
    RelayValidationError,
    # Connectivity + low-level client
    check_connection,
    request,
    # Validation (exposed for callers + tests)
    validate_request,
)
from aexp.airgapped.client import RelayClient

__all__ = [
    "ALLOWED",
    "OpSpec",
    "RelayClient",
    "RelayDownError",
    "RelayError",
    "RelayRejectedError",
    "RelayResult",
    "RelayTimeoutError",
    "RelayValidationError",
    "check_connection",
    "request",
    "validate_request",
]
