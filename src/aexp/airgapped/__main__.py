"""Module entry point for ``python -m aexp.airgapped``.

Delegates to :func:`aexp.airgapped._relay.main`, which runs the Typer
``airgapped_app`` (``status`` / ``pull`` / ``push`` / ``fetch`` /
``repo-status`` / ``rebase`` / ``wandb-sync``).

Lives in ``__main__.py`` (not ``__init__.py``) because Python's ``-m``
only finds a package entry point when the package contains a literal
``__main__.py`` file. The same CLI is also reachable as the
``aexp airgapped`` subcommand of the top-level CLI.
"""
from __future__ import annotations

import sys

from aexp.airgapped._relay import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
