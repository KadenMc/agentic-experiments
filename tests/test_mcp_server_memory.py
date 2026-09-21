"""The MCP server process must not reserve a per-core BLAS buffer pool.

OpenBLAS reserves a per-thread buffer pool sized to the machine's core count
and charges it as committed address space the moment numpy is imported, before
any work happens. On a 16-core machine that is ~490 MB per process, against a
working set of a few tens of MB. Claude Code spawns one ``aexp`` MCP server per
session, so uncapped this scales into gigabytes of commit for a process that
does no numerical work at all.

``aexp.install._MCP_SERVER_ENV`` caps the pool in the *launcher* environment.
That placement is load-bearing and is what these tests actually guard -- see the
comment on that constant.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest

from aexp.install import _MCP_SERVER_ENV

_THREAD_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

# Measured in a *fresh* interpreter: the reservation happens once, when OpenBLAS
# loads, so it cannot be observed by importing twice in one process.
_PROBE = textwrap.dedent(
    """
    import psutil

    p = psutil.Process()

    def _committed():
        mi = p.memory_info()
        # Windows: `private` is the commit charge, which is the limit that binds.
        # Elsewhere: the reservation is untouched, so it shows in vms, not rss.
        return getattr(mi, "private", None) or mi.vms

    base = _committed()
    import aexp.mcp_server  # noqa: F401
    print((_committed() - base) // 1048576)
    """
)


def _import_cost_mb(*, capped: bool) -> int:
    """Private-commit delta (MB) of importing ``aexp.mcp_server`` in a fresh process."""
    env = dict(os.environ)
    for var in _THREAD_VARS:
        env.pop(var, None)
    if capped:
        env.update({k: v for k, v in _MCP_SERVER_ENV.items() if k.endswith("_NUM_THREADS")})
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return int(proc.stdout.strip().splitlines()[-1])


def test_mcp_server_env_caps_the_blas_thread_pool() -> None:
    """The shipped launcher env sets a cap that OpenBLAS will actually honour.

    ``OPENBLAS_NUM_THREADS`` and ``OMP_NUM_THREADS`` are each independently
    sufficient. ``MKL_NUM_THREADS`` is deliberately absent: it was measured to
    have no effect on an OpenBLAS-backed numpy, so shipping it would imply a
    guarantee that has not been verified.
    """
    assert _MCP_SERVER_ENV["OPENBLAS_NUM_THREADS"] == "1"
    assert _MCP_SERVER_ENV["OMP_NUM_THREADS"] == "1"
    assert "MKL_NUM_THREADS" not in _MCP_SERVER_ENV


@pytest.mark.slow
def test_capping_threads_actually_shrinks_the_import() -> None:
    """End-to-end: the cap is what makes the import cheap, on this machine.

    Self-calibrating rather than threshold-based. The reservation scales with
    core count, so a fixed megabyte ceiling would be flaky across machines (and
    absent entirely on an MKL-backed numpy). Instead: measure uncapped first,
    and only assert if there is a reservation here worth asserting about.
    """
    pytest.importorskip("psutil")
    if importlib.util.find_spec("numpy") is None:
        pytest.skip(
            "numpy is not installed, so no BLAS arena is ever reserved and there is "
            "nothing to measure. signac does not require numpy, so a minimal install "
            "-- including CI -- never reproduces this. It is the numerical runtime "
            "environment the server actually runs in that does."
        )

    uncapped = _import_cost_mb(capped=False)
    if uncapped < 200:
        pytest.skip(
            f"no measurable BLAS reservation here: uncapped import = {uncapped} MB "
            f"on {os.cpu_count()} cores. The reservation scales with core count, so a "
            f"small CI runner lands here legitimately -- on those machines the shipped-env "
            f"test above is what guards the fix."
        )

    capped = _import_cost_mb(capped=True)
    assert capped < uncapped / 2, (
        f"launcher env failed to cap the BLAS arena: {uncapped} MB uncapped vs "
        f"{capped} MB capped. If the caps were moved into aexp/mcp_server.py, "
        f"this is exactly how that fails -- numpy is already loaded via "
        f"aexp/__init__.py before that module's body runs."
    )
