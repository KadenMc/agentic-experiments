"""Airgapped-compute support: file-queue bridge between no-internet compute
node and internet-having login node.

Designed for environments where the agent's runtime is network-isolated
(common at secure HPC sites) but a sibling node sharing the user's home
filesystem has internet access. The relay lets the agent run whitelisted
commands (git operations, optional consent-required ops) on the sibling
node via a file-based queue.

Public surface:

- :class:`RelayClient` — high-level client; recommended for most callers.
  Wraps the low-level :func:`request` with semantic methods
  (``pull``, ``push``, ``fetch``, ``status``, ``rebase``) and designs
  out the F7/F8 arg-passing frictions documented in ``aexp.md``.
- :func:`request` — low-level escape hatch for callers who want full
  control over per-call queue / cwd / args.
- :class:`RelayResult`, :class:`RelayError` (+ subclasses) — result and
  error types.

This subpackage is **not** imported at ``aexp`` package init — users
opt in by importing ``aexp.airgapped``. The relay is genuinely
infrastructure-specific (most users don't have airgapped compute
constraints), so default-installing it would be bloat.

See ``docs/airgapped.md`` for the daemon bootstrap recipe + full
protocol description. For the reference implementation that motivated
this port, see ``electricrag/dev/relay.py`` (preserved upstream) and
the 56-test suite in ``electricrag/tests/dev/test_relay.py``.
"""
from __future__ import annotations

from aexp.airgapped._relay import (
    # Whitelist + spec
    ALLOWED,
    # Low-level client + queue helpers
    DEFAULT_QUEUE,
    OpSpec,
    # Result + errors
    RelayCrashedError,
    RelayDownError,
    RelayError,
    RelayRejectedError,
    RelayResult,
    RelayTimeoutError,
    RelayValidationError,
    ensure_queue,
    request,
    # Validation (exposed for the daemon side + tests)
    validate_request,
)
from aexp.airgapped.client import RelayClient

__all__ = [
    "ALLOWED",
    "DEFAULT_QUEUE",
    "OpSpec",
    "RelayClient",
    "RelayCrashedError",
    "RelayDownError",
    "RelayError",
    "RelayRejectedError",
    "RelayResult",
    "RelayTimeoutError",
    "RelayValidationError",
    "ensure_queue",
    "request",
    "validate_request",
]
