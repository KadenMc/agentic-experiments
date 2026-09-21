"""``import aexp`` must not drag in the numerical stack.

``aexp/__init__.py`` used to import the run-store layer at module scope, and
``aexp.runs`` -> ``signac`` -> ``synced_collections`` -> ``numpy``. Python
initializes a parent package before any submodule, so that cost landed on every
caller of every submodule -- including someone who only wanted
``from aexp.utils.atomic import atomic_write``. A library should not force a
heavy numerical dependency on a caller importing a file-write helper.

The contract these tests hold is "signac and numpy are not imported", not a
megabyte threshold: the headline number came from an OpenBLAS arena that scales
with core count, so a size assertion would be machine-dependent and flaky.
"""
from __future__ import annotations

import pkgutil
import subprocess
import sys
import textwrap

import pytest

import aexp
import aexp.hooks
from aexp import _LAZY, _LAZY_EXPORTS

# Discovered, never hardcoded. Every hook is a `python -m aexp.hooks.<mod>`
# process, and kb_write_guard is wired to Write|Edit|MultiEdit -- one process
# per file edit. A hand-maintained list would silently miss the next hook
# somebody adds, which is precisely how the cost this test guards got in.
_HOOK_MODULES = sorted(
    f"aexp.hooks.{m.name}"
    for m in pkgutil.iter_modules(aexp.hooks.__path__)
    if not m.name.startswith("_")
)

# Resolving these needs the optional [wandb] extra installed.
_WANDB_NAMES = frozenset(_LAZY_EXPORTS["aexp.trackers.wandb_adapter"])


def _in_fresh_interpreter(body: str) -> str:
    """Run ``body`` in a fresh interpreter; return stdout. Import state matters."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "module",
    [
        "aexp",
        "aexp.cli",
        "aexp.utils.atomic",
        "aexp.utils.paths",
        "aexp.schema",
        *_HOOK_MODULES,
    ],
)
def test_importing_does_not_pull_the_numerical_stack(module: str) -> None:
    """The actual contract. ``aexp.utils.atomic`` is the motivating case.

    ``aexp.cli`` is the other one worth naming: it transitively imports
    ``install``, ``linking``, ``queue``, ``runs`` and ``trackers.base`` --
    every leaf module that talks to signac -- so it is the surface most
    likely to regress if one of those five goes back to a module-scope
    ``import signac``.
    """
    loaded = _in_fresh_interpreter(
        f"""
        import sys
        import {module}
        heavy = [m for m in ("numpy", "signac", "synced_collections") if m in sys.modules]
        print(",".join(heavy))
        """
    )
    assert loaded == "", f"importing {module} pulled in: {loaded}"


def test_hook_discovery_actually_found_the_hooks() -> None:
    """Guard the guard: an empty discovery would make the test above vacuous.

    ``kb_write_guard`` is named explicitly because it is the load-bearing one --
    it runs on every Write/Edit/MultiEdit. If a refactor moves or renames it,
    fail here loudly rather than quietly covering nothing.
    """
    assert _HOOK_MODULES, "no hook modules discovered; the import guard is vacuous"
    assert "aexp.hooks.kb_write_guard" in _HOOK_MODULES


# ---------------------------------------------------------------------------
# The lazy table must stay honest
# ---------------------------------------------------------------------------


def test_every_public_name_is_lazily_resolvable() -> None:
    """``__all__`` and the lazy table cannot drift apart.

    ``_LAZY`` is derived from ``_LAZY_EXPORTS``, so a name added to ``__all__``
    without a home in the grouping is caught here rather than at a user's first
    ``from aexp import ...``.
    """
    declared = set(aexp.__all__) - {"__version__"}
    missing = declared - set(_LAZY)
    assert not missing, f"in __all__ but not resolvable lazily: {sorted(missing)}"


def test_lazy_names_resolve_to_their_defining_module() -> None:
    """Every entry really resolves, and to the object the table claims."""
    import importlib

    for name, module_name in _LAZY.items():
        if name in _WANDB_NAMES:
            continue  # optional extra; covered separately
        resolved = getattr(aexp, name)
        expected = getattr(importlib.import_module(module_name), name)
        assert resolved is expected, f"aexp.{name} is not {module_name}.{name}"


def test_wandb_surface_is_lazy_but_present() -> None:
    """The wandb-dependent names were already lazy; they must stay that way."""
    pytest.importorskip("wandb")
    for name in _WANDB_NAMES:
        assert getattr(aexp, name) is not None


# ---------------------------------------------------------------------------
# Backwards compatibility of the public surface
# ---------------------------------------------------------------------------


def test_submodule_attribute_access_still_works() -> None:
    """``import aexp; aexp.runs`` worked when submodules were imported eagerly.

    The eager imports bound them as attributes as a side effect. Dropping the
    eager imports would have silently broken that, so ``__getattr__`` falls back
    to importing ``aexp.<name>``.
    """
    out = _in_fresh_interpreter(
        """
        import aexp
        print(aexp.runs.__name__, aexp.schema.__name__, aexp.utils.__name__)
        """
    )
    assert out == "aexp.runs aexp.schema aexp.utils"


def test_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="no attribute 'definitely_not_here'"):
        aexp.definitely_not_here  # noqa: B018


def test_missing_third_party_dep_is_not_flattened_into_attribute_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real missing dependency must stay an ImportError.

    The submodule fallback catches ``ModuleNotFoundError``, so the danger is
    swallowing "wandb is not installed" and reporting "aexp has no attribute
    trackers" instead -- an error that points at the wrong problem entirely.
    Only the *requested* module being absent may become an AttributeError.
    """
    import importlib

    def _fake_import(name: str) -> object:
        raise ModuleNotFoundError("No module named 'some_dep'", name="some_dep")

    monkeypatch.setattr(importlib, "import_module", _fake_import)
    with pytest.raises(ModuleNotFoundError, match="some_dep"):
        aexp.__getattr__("never_cached_submodule")


def test_resolved_names_are_cached_on_the_module() -> None:
    """First access populates ``globals()`` so repeat lookups skip __getattr__."""
    out = _in_fresh_interpreter(
        """
        import aexp
        print("create_run" in vars(aexp))
        aexp.create_run
        print("create_run" in vars(aexp))
        """
    )
    assert out.split() == ["False", "True"]


def test_dir_includes_lazy_names() -> None:
    names = dir(aexp)
    assert "create_run" in names
    assert "validate_repo" in names
    assert names == sorted(names), "dir() should be sorted"
