"""Claude Code hooks — shipped inside the ``aexp`` package.

Each hook is a small ``python -m aexp.hooks.<name>`` entry point referenced
from the ``.claude/settings.json`` that :func:`aexp.install.install_scaffold`
writes into a consumer repo. The hook scripts **do not** get copied into the
repo — they live here and upgrade with ``pip install -U agentic-experiments``.

Design notes
------------

- Hooks derive the repo root from ``os.getcwd()``. Claude Code invokes hooks
  with ``cwd`` set to the project root. ``aexp.utils.paths.find_repo_root``
  is used as a fallback when that assumption does not hold.
- Hooks never subprocess into ``scripts/`` files. Validation calls
  :func:`aexp.kb_validate.validate_kb` in-process.
- Limina upstream's telemetry has been intentionally stripped — ``aexp`` does
  not emit to Limina's sink.
"""
