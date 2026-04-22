"""Shared pytest fixtures for aexp tests."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
VENDOR_LIMINA = PACKAGE_ROOT / "src" / "aexp" / "vendor" / "limina"


@pytest.fixture
def vendored_limina_tree() -> Path:
    """Absolute path to the vendored Limina snapshot in this repo."""
    assert VENDOR_LIMINA.is_dir(), f"vendored Limina missing at {VENDOR_LIMINA}"
    return VENDOR_LIMINA


@pytest.fixture
def limina_project(tmp_path: Path) -> Path:
    """Copy the vendored Limina snapshot into a tmp dir.

    Gives each test an isolated ``PROJECT_ROOT`` — the ported hooks derive
    their root from ``Path(__file__).resolve().parents[2]``, so running a
    copied hook sets ``PROJECT_ROOT`` to this tmp dir.
    """
    dest = tmp_path / "limina_project"
    shutil.copytree(VENDOR_LIMINA, dest)
    return dest


@pytest.fixture
def python_exe() -> str:
    """Path to the Python interpreter running pytest — used to exec subprocess hooks."""
    return sys.executable
