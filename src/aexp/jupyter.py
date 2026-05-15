"""Live Jupyter session introspection.

The premise: an agent that connects to a Jupyter kernel — local laptop or
remote compute node — needs to be able to answer "which Jupyter am I sitting
on, what else does the user have running, and what is happening inside each
session?" without consulting any persistent registry.

Every fact the agent needs is recoverable on demand:

- Technical identity (port, token, root_dir, PID) — from
  ``jupyter_server.serverapp.list_running_servers()`` and ``JPY_PARENT_PID``.
- Process identity (kernel id, cgroup) — from environment + ``/proc/self/cgroup``.
- SLURM context (when present) — from ``/proc/self/cgroup`` cgroup hierarchy
  plus ``squeue`` / ``scontrol``.
- Sibling Jupyters — also ``list_running_servers()``; on shared-home HPC this
  enumerates cluster-wide because the runtime dir lives in ``$HOME``.
- Attached notebooks + kernel state — Jupyter HTTP ``/api/sessions``.
- GPU residents — ``nvidia-smi --query-compute-apps``.

Every probe degrades gracefully when its prerequisite is missing: laptop with
no SLURM gets ``slurm=None``; non-Linux gets ``cgroup=None`` without raising;
machine with no ``nvidia-smi`` gets ``gpu_processes=[]``.

The module is intentionally side-effect-free: it never writes a file, never
mutates kernel state, never executes user code. Designed to be called as
``from aexp.jupyter import init; init()`` from inside any kernel.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SlurmContext(BaseModel):
    """SLURM job context discovered from cgroup + ``squeue`` / ``scontrol``."""

    job_id: str
    job_name: str | None = None
    user: str | None = None
    state: str | None = None
    runtime: str | None = None
    time_limit: str | None = None
    nodelist: str | None = None
    partition: str | None = None
    submit_time: str | None = None
    start_time: str | None = None


class GpuProcess(BaseModel):
    """A single process holding GPU memory, per ``nvidia-smi``."""

    pid: int
    used_memory: str
    gpu_uuid: str | None = None


class SiblingSession(BaseModel):
    """A Jupyter server *other than the current one*, discovered via
    ``list_running_servers``."""

    port: int | None = None
    url: str
    token: str | None = None
    root_dir: str | None = None
    pid: int | None = None
    hostname: str | None = None


class SessionInfo(BaseModel):
    """Live introspection of the kernel + Jupyter the agent is connected to."""

    introspected_at: datetime
    # Technical identity
    jupyter_url: str | None = None
    jupyter_port: int | None = None
    jupyter_token: str | None = None
    jupyter_root_dir: str | None = None
    jupyter_pid: int | None = None
    # Process identity
    kernel_id: str | None = None
    cgroup: str | None = None
    # SLURM
    slurm: SlurmContext | None = None
    # Host
    hostname: str
    # Live semantics
    attached_notebooks: list[str] = Field(default_factory=list)
    gpu_processes: list[GpuProcess] = Field(default_factory=list)
    cluster_siblings: list[SiblingSession] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal: import guard
# ---------------------------------------------------------------------------


def _require_jupyter_server() -> Any:
    """Return ``jupyter_server.serverapp`` or raise a friendly ImportError."""
    try:
        from jupyter_server import serverapp  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "aexp.jupyter requires the 'jupyter' extra. "
            "Install with: pip install 'agentic-experiments[jupyter]'"
        ) from exc
    return serverapp


# ---------------------------------------------------------------------------
# Probes — each is independently testable and silently degrades on failure
# ---------------------------------------------------------------------------


_CGROUP_JOB_RE = re.compile(r"/job_(\d+)(?:/|$)")


def _read_cgroup() -> str | None:
    """Return the raw cgroup line for the current process, or ``None``.

    Linux only. Returns the *most descriptive* (longest path) cgroup line —
    on cgroup v1 there's one line per controller; on v2 there's a single
    line. The longest path is the one that typically carries SLURM's
    ``job_<id>`` segment.
    """
    path = Path("/proc/self/cgroup")
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    # Each line looks like "0::/user.slice/.../job_12345/step_batch/...".
    # Take the line with the longest path component.
    def _path_of(line: str) -> str:
        parts = line.split(":", 2)
        return parts[2] if len(parts) == 3 else ""

    best = max(lines, key=lambda ln: len(_path_of(ln)))
    return best.strip() or None


def _parse_slurm_job_id_from_cgroup(cgroup_line: str | None) -> str | None:
    if not cgroup_line:
        return None
    m = _CGROUP_JOB_RE.search(cgroup_line)
    if m:
        return m.group(1)
    return None


def _run_capture(argv: list[str], timeout: float = 5.0) -> str | None:
    """Run ``argv``, return stdout on success, ``None`` on any failure.

    Returns ``None`` if the executable isn't on PATH (silenced), the command
    exits non-zero, or it times out — callers want a tri-state (data /
    no-data) rather than exceptions.
    """
    exe = argv[0]
    if shutil.which(exe) is None:
        return None
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def probe_slurm() -> SlurmContext | None:
    """Best-effort SLURM context for the current process.

    Resolution order for job id:
    1. ``/proc/self/cgroup`` ``job_<id>`` segment (most reliable — survives
       env-var stripping by Jupyter kernels).
    2. ``SLURM_JOB_ID`` env var fallback.

    If neither yields an id, returns ``None``. If an id is found, also
    enriches via ``squeue --job <id>`` and ``scontrol show job <id>`` —
    failures of those enrichment commands are swallowed silently.
    """
    cgroup_line = _read_cgroup()
    job_id = _parse_slurm_job_id_from_cgroup(cgroup_line) or os.environ.get("SLURM_JOB_ID")
    if not job_id:
        return None

    ctx = SlurmContext(job_id=job_id)

    # squeue -h -j <id> -o "%j|%T|%M|%l|%R|%P|%u"
    sq_out = _run_capture(
        ["squeue", "-h", "-j", job_id, "-o", "%j|%T|%M|%l|%R|%P|%u"]
    )
    if sq_out:
        fields = sq_out.strip().split("|")
        if len(fields) == 7:
            stripped = tuple(f.strip() or None for f in fields)
            (
                ctx.job_name,
                ctx.state,
                ctx.runtime,
                ctx.time_limit,
                ctx.nodelist,
                ctx.partition,
                ctx.user,
            ) = stripped

    sc_out = _run_capture(["scontrol", "show", "job", job_id])
    if sc_out:
        sc_dict = _parse_scontrol(sc_out)
        ctx.submit_time = sc_dict.get("SubmitTime") or ctx.submit_time
        ctx.start_time = sc_dict.get("StartTime") or ctx.start_time
        # scontrol values fill in gaps left by squeue
        ctx.job_name = ctx.job_name or sc_dict.get("JobName")
        ctx.nodelist = ctx.nodelist or sc_dict.get("NodeList")
        ctx.partition = ctx.partition or sc_dict.get("Partition")
        ctx.user = ctx.user or sc_dict.get("UserId")

    return ctx


def _parse_scontrol(text: str) -> dict[str, str]:
    """Parse the ``key=value`` pairs that ``scontrol show job`` emits.

    The output looks like::

        JobId=12345 JobName=train ... UserId=alice(1001) ...
        StartTime=2026-05-14T09:00:00 SubmitTime=2026-05-14T08:55:00 ...

    Whitespace separates pairs, ``=`` separates key from value. Values
    themselves may contain ``/`` but never whitespace or ``=``.
    """
    out: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            key, _, val = token.partition("=")
            if key and val:
                out[key] = val
    return out


def probe_gpu() -> list[GpuProcess]:
    """Return GPU processes from ``nvidia-smi``, or ``[]`` if unavailable."""
    out = _run_capture(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory,gpu_uuid",
            "--format=csv,noheader,nounits",
        ]
    )
    if not out:
        return []
    procs: list[GpuProcess] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        used_mem = parts[1] + " MiB" if parts[1].isdigit() else parts[1]
        gpu_uuid = parts[2] if len(parts) >= 3 and parts[2] else None
        procs.append(GpuProcess(pid=pid, used_memory=used_mem, gpu_uuid=gpu_uuid))
    return procs


# ---------------------------------------------------------------------------
# Server discovery — list_running_servers + HTTP /api/sessions
# ---------------------------------------------------------------------------


def _list_running_servers() -> list[dict[str, Any]]:
    """Wrap ``list_running_servers`` so the rest of the module deals in dicts.

    Returns ``[]`` on any failure (e.g. the runtime dir was wiped). Each
    entry is the dict Jupyter writes to its runtime ``jpserver-<pid>.json``
    file — keys vary by version, but include ``url``, ``token``,
    ``root_dir``, ``pid``, ``port``, ``hostname``.
    """
    try:
        serverapp = _require_jupyter_server()
    except ImportError:
        return []
    try:
        return list(serverapp.list_running_servers())
    except Exception:
        return []


def _pid_alive(pid: int) -> bool:
    """Cheap liveness check.

    Linux/macOS: ``os.kill(pid, 0)`` raises ``ProcessLookupError`` for dead
    pids without actually signalling. Windows lacks signal 0 semantics, so
    we use ``OpenProcess`` via ``psutil`` if available, falling back to
    ``True`` (don't prune what we can't verify).
    """
    if pid <= 0:
        return False
    if hasattr(os, "kill"):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but is owned by someone else — treat as alive.
            return True
        except OSError:
            return False
        return True
    return True  # pragma: no cover — non-POSIX fallback


def _current_server_info() -> dict[str, Any] | None:
    """Identify the Jupyter server hosting the *current* kernel.

    Uses ``JPY_PARENT_PID`` (set by Jupyter for every kernel subprocess) to
    match against ``list_running_servers``. Falls back to ``None`` if the
    env var is unset (i.e. we're not inside a kernel).
    """
    parent_pid = os.environ.get("JPY_PARENT_PID")
    if not parent_pid:
        return None
    try:
        parent_pid_int = int(parent_pid)
    except ValueError:
        return None
    for entry in _list_running_servers():
        if entry.get("pid") == parent_pid_int:
            return entry
    return None


def discover_other_servers(*, prune_stale: bool = True) -> list[SiblingSession]:
    """Enumerate Jupyter servers visible from this process *excluding* current.

    On HPC where ``$HOME`` (and therefore ``JUPYTER_RUNTIME_DIR``) is shared
    across compute nodes, this enumerates cluster-wide. On a laptop with no
    shared home, only local Jupyters appear.

    Parameters
    ----------
    prune_stale : bool
        If True (default), drop entries whose ``pid`` is no longer alive on
        the local node. Entries from *other* nodes always pass through —
        we can't probe their pids from here.
    """
    current_pid = os.environ.get("JPY_PARENT_PID")
    try:
        current_pid_int = int(current_pid) if current_pid else None
    except ValueError:
        current_pid_int = None

    local_host = socket.gethostname()

    siblings: list[SiblingSession] = []
    for entry in _list_running_servers():
        pid = entry.get("pid")
        host = entry.get("hostname") or local_host
        if current_pid_int is not None and pid == current_pid_int:
            continue
        if prune_stale and host == local_host and isinstance(pid, int) and not _pid_alive(pid):
            continue
        siblings.append(
            SiblingSession(
                port=entry.get("port"),
                url=entry.get("url", ""),
                token=entry.get("token") or None,
                root_dir=entry.get("root_dir") or None,
                pid=pid if isinstance(pid, int) else None,
                hostname=host,
            )
        )
    return siblings


def _http_get_json(url: str, token: str | None, *, timeout: float = 3.0) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — explicit http(s) only
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def describe_server(url: str, token: str | None) -> dict[str, Any]:
    """Fetch ``/api/sessions`` + ``/api/kernels`` summary for a Jupyter server.

    Returns a dict with ``attached_notebooks`` (sorted list of notebook
    paths) and ``kernels`` (one entry per kernel with id, state, last
    activity). Pure HTTP — does NOT execute any code on the remote kernel.
    Returns ``{}`` if the server is unreachable.
    """
    base = url.rstrip("/")
    sessions = _http_get_json(f"{base}/api/sessions", token) or []
    kernels = _http_get_json(f"{base}/api/kernels", token) or []

    if not isinstance(sessions, list):
        sessions = []
    if not isinstance(kernels, list):
        kernels = []

    notebooks: list[str] = []
    for s in sessions:
        nb = (s.get("notebook") or {}).get("path") or s.get("path")
        if nb:
            notebooks.append(nb)

    kernel_summaries = [
        {
            "id": k.get("id"),
            "name": k.get("name"),
            "execution_state": k.get("execution_state"),
            "last_activity": k.get("last_activity"),
            "connections": k.get("connections"),
        }
        for k in kernels
    ]
    return {
        "attached_notebooks": sorted(set(notebooks)),
        "kernels": kernel_summaries,
    }


def _attached_notebooks_for_current() -> list[str]:
    """Pull attached notebook paths from the *current* server's /api/sessions."""
    current = _current_server_info()
    if not current:
        return []
    summary = describe_server(current.get("url", ""), current.get("token"))
    return summary.get("attached_notebooks", [])  # type: ignore[no-any-return]


def _current_kernel_id() -> str | None:
    """Best-effort current-kernel id.

    Tries the IPython kernel app (``IPKernelApp.instance().session.session``
    is the kernel id-ish in newer versions; older ones expose it via the
    connection file). Returns ``None`` outside of an IPython kernel.
    """
    try:
        from ipykernel.kernelapp import IPKernelApp  # noqa: PLC0415
    except ImportError:
        return None
    if not IPKernelApp.initialized():
        return None
    app = IPKernelApp.instance()
    # The connection file's stem is "kernel-<uuid>.json" in modern Jupyter.
    cf = getattr(app, "connection_file", None)
    if cf:
        stem = Path(cf).stem
        if stem.startswith("kernel-"):
            return stem[len("kernel-"):]
        return stem
    return None


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def init(*, discover: bool = True) -> SessionInfo:
    """Run every introspection probe; return a populated :class:`SessionInfo`.

    Side-effect free. Designed to be called from inside any kernel:

        from aexp.jupyter import init
        info = init()
        print(info.model_dump_json(indent=2))

    Parameters
    ----------
    discover : bool
        If True (default), also populate ``cluster_siblings``. Costs one
        ``list_running_servers`` call plus a liveness probe per entry.
    """
    cgroup_line = _read_cgroup()
    current = _current_server_info()
    slurm = probe_slurm()
    gpu = probe_gpu()
    siblings = discover_other_servers() if discover else []

    info = SessionInfo(
        introspected_at=datetime.now(UTC),
        hostname=socket.gethostname(),
        kernel_id=_current_kernel_id(),
        cgroup=cgroup_line,
        slurm=slurm,
        gpu_processes=gpu,
        cluster_siblings=siblings,
    )

    if current:
        info.jupyter_url = current.get("url")
        info.jupyter_port = current.get("port")
        info.jupyter_token = current.get("token") or None
        info.jupyter_root_dir = current.get("root_dir") or None
        info.jupyter_pid = current.get("pid")
        info.attached_notebooks = _attached_notebooks_for_current()
    else:
        # Fall back to JPY_PARENT_PID even if list_running_servers came up
        # empty — at least record that we *are* in a kernel.
        parent_pid = os.environ.get("JPY_PARENT_PID")
        if parent_pid:
            try:
                info.jupyter_pid = int(parent_pid)
            except ValueError:
                pass

    return info


def whoami(*, discover: bool = True) -> SessionInfo:
    """Alias for :func:`init` — matches the "which session am I in?" mental
    model.
    """
    return init(discover=discover)


# ---------------------------------------------------------------------------
# CLI entry helper (used by ``aexp jupyter ...`` verbs)
# ---------------------------------------------------------------------------


def _print_info_human(info: SessionInfo) -> None:
    """Render a SessionInfo to stdout in a compact human form.

    Used by the ``aexp jupyter init`` / ``whoami`` CLI verbs. JSON output
    is handled by the caller via ``info.model_dump_json()``.
    """
    print(f"hostname: {info.hostname}")
    if info.jupyter_url:
        print(f"jupyter:  {info.jupyter_url}")
        if info.jupyter_port is not None:
            print(f"  port:        {info.jupyter_port}")
        if info.jupyter_root_dir:
            print(f"  root_dir:    {info.jupyter_root_dir}")
        if info.jupyter_pid:
            print(f"  pid:         {info.jupyter_pid}")
    else:
        print("jupyter:  (no server identified for this process)")
    if info.kernel_id:
        print(f"kernel:   {info.kernel_id}")
    if info.slurm:
        print(f"slurm:    job_id={info.slurm.job_id} name={info.slurm.job_name or '?'} "
              f"state={info.slurm.state or '?'} runtime={info.slurm.runtime or '?'} "
              f"nodelist={info.slurm.nodelist or '?'}")
    else:
        print("slurm:    (not under SLURM)")
    if info.attached_notebooks:
        print("notebooks:")
        for nb in info.attached_notebooks:
            print(f"  - {nb}")
    if info.gpu_processes:
        print("gpu:")
        for p in info.gpu_processes:
            print(f"  - pid={p.pid} mem={p.used_memory}")
    if info.cluster_siblings:
        print("siblings:")
        for s in info.cluster_siblings:
            print(f"  - {s.url} (port={s.port}, pid={s.pid}, host={s.hostname})")
    sys.stdout.flush()
