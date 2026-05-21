"""Shared pytest fixtures for aexp tests."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PACKAGE_ROOT / "src" / "aexp" / "vendor" / "limina"


@pytest.fixture
def vendored_tree() -> Path:
    """Absolute path to the vendored research-harness snapshot in this repo."""
    assert VENDOR_ROOT.is_dir(), f"vendored research harness missing at {VENDOR_ROOT}"
    return VENDOR_ROOT


@pytest.fixture
def scaffold_project(tmp_path: Path) -> Path:
    """Copy the vendored research-harness snapshot into a tmp dir.

    Gives each test an isolated ``PROJECT_ROOT`` — the ported hooks derive
    their root from ``Path(__file__).resolve().parents[2]``, so running a
    copied hook sets ``PROJECT_ROOT`` to this tmp dir.
    """
    dest = tmp_path / "scaffold_project"
    shutil.copytree(VENDOR_ROOT, dest)
    return dest


@pytest.fixture
def python_exe() -> str:
    """Path to the Python interpreter running pytest — used to exec subprocess hooks."""
    return sys.executable
