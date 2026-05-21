"""Deprecated alias for :mod:`aexp.kb_io`.

``aexp.limina_io`` was renamed to :mod:`aexp.kb_io` during the limina
de-brand. This shim re-exports the new module so existing imports keep
working; importing it emits a :class:`DeprecationWarning`. It will be
removed in a future release -- migrate to ``from aexp.kb_io import ...``.
"""
from __future__ import annotations

import warnings as _warnings

from aexp.kb_io import *  # noqa: F401,F403  (back-compat re-export)

_warnings.warn(
    "aexp.limina_io has been renamed to aexp.kb_io; "
    "update imports to `from aexp.kb_io import ...`.",
    DeprecationWarning,
    stacklevel=2,
)
