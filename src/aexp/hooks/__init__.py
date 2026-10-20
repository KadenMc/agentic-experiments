"""Claude Code hooks -- shipped inside the ``aexp`` package.

Each hook is a small ``python -m aexp.hooks.<name>`` entry point referenced
from the ``.claude/settings.json`` that :func:`aexp.install.install_scaffold`
writes into a consumer repo. The hook scripts **do not** get copied into the
repo -- they live here and upgrade with ``pip install -U agentic-experiments``.

Design notes
------------

- **Hooks find their checkout from the event, not from ``os.getcwd()``.**
  Claude Code runs a hook in the session's current directory, which can be a
  subfolder or a worktree. In a desktop-app worktree session it is the main
  checkout, and the worktree arrives only in the input's ``cwd``.
  :mod:`aexp.hooks._project` resolves the checkout an event concerns (the
  session's current directory for Stop and SessionStart, the edited file for
  writes) and accepts it only when it belongs to the session's own
  repository, named by ``CLAUDE_PROJECT_DIR``, worktrees included. A
  session-level hook otherwise checks the project, and a write hook leaves a
  file from another repository alone.
- **Hooks read and write UTF-8.** Claude Code sends a hook's payload as UTF-8
  and decodes its stdout and stderr as UTF-8, while Python would give those
  pipes cp1252 on Windows. Every hook's ``main()`` calls
  :func:`aexp.hooks._stdio.use_utf8` before any I/O, so a non-ASCII path in a
  payload, or a non-ASCII line in a file a hook relays, arrives intact.
- Hooks never subprocess into ``scripts/`` files. Validation calls
  :func:`aexp.kb_validate.validate_kb` in-process.
- The upstream harness's telemetry has been intentionally stripped -- ``aexp``
  does not emit to any external sink.
- **Keep hooks cheap to import.** ``kb_write_guard`` is wired to
  ``Write|Edit|MultiEdit``, so one of these processes is spawned on *every file
  edit an agent makes*. Anything a hook imports is therefore paid per edit, not
  per session. This is not hypothetical: while ``aexp/__init__.py`` still
  imported the run-store layer eagerly (``aexp.runs`` -> signac -> numpy),
  merely importing a hook cost ~506 MB of private commit and over a second of
  wall clock, because Python initializes a parent package before any of its
  submodules. Nothing in the repo caught it; it was found by accident while
  profiling something else.

  Package init is lazy now, and a hook may freely import ``aexp.utils.*``,
  ``aexp.schema`` and ``aexp.kb_validate``. If a new hook needs the run store,
  import it **inside the function that uses it**, never at module scope.
  ``tests/test_lazy_init.py`` enforces this by asserting that importing a hook
  pulls in neither signac nor numpy. It *discovers* the modules in this package
  rather than listing them, so a new hook is covered without anyone remembering
  to register it.
"""
