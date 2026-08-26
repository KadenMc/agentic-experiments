"""Tests for ``aexp.jupyter`` introspection helpers.

Every probe is independently tested with mocks so the suite is hermetic
(no SLURM, no nvidia-smi, no live Jupyter required).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from aexp import jupyter as jupyter_mod
from aexp.jupyter import (
    GpuProcess,
    SessionInfo,
    SiblingSession,
    SlurmContext,
    describe_server,
    discover_other_servers,
    init,
    probe_gpu,
    probe_slurm,
    whoami,
)

# ---------------------------------------------------------------------------
# probe_slurm
# ---------------------------------------------------------------------------


CGROUP_WITH_JOB = (
    "0::/system.slice/slurmstepd.scope/job_12345/step_batch/task_0"
)
CGROUP_NO_JOB = "0::/user.slice/user-1001.slice/session-3.scope"


def test_parse_slurm_job_id_from_cgroup_extracts_id() -> None:
    assert (
        jupyter_mod._parse_slurm_job_id_from_cgroup(CGROUP_WITH_JOB) == "12345"
    )


def test_parse_slurm_job_id_from_cgroup_returns_none_without_job() -> None:
    assert jupyter_mod._parse_slurm_job_id_from_cgroup(CGROUP_NO_JOB) is None
    assert jupyter_mod._parse_slurm_job_id_from_cgroup(None) is None


def test_probe_slurm_returns_none_when_no_cgroup_or_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    with patch.object(jupyter_mod, "_read_cgroup", return_value=None):
        assert probe_slurm() is None


def test_probe_slurm_picks_up_env_var_when_cgroup_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "99999")
    with patch.object(jupyter_mod, "_read_cgroup", return_value=CGROUP_NO_JOB), \
         patch.object(jupyter_mod, "_run_capture", return_value=None):
        ctx = probe_slurm()
    assert ctx is not None
    assert ctx.job_id == "99999"


def test_probe_slurm_full_chain_populates_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cgroup reveals the job + squeue + scontrol return data, every
    field is populated."""
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    squeue_out = "train|RUNNING|01:23:45|04:00:00|testnode01|gpu|alice\n"
    scontrol_out = (
        "JobId=12345 JobName=train UserId=alice(1001) Partition=gpu "
        "NodeList=testnode01 SubmitTime=2026-05-14T08:00:00 "
        "StartTime=2026-05-14T08:01:00\n"
    )

    def fake_run(argv: list[str], timeout: float = 5.0) -> str:
        if argv[0] == "squeue":
            return squeue_out
        if argv[0] == "scontrol":
            return scontrol_out
        return ""

    with patch.object(jupyter_mod, "_read_cgroup", return_value=CGROUP_WITH_JOB), \
         patch.object(jupyter_mod, "_run_capture", side_effect=fake_run):
        ctx = probe_slurm()
    assert ctx is not None
    assert ctx.job_id == "12345"
    assert ctx.job_name == "train"
    assert ctx.state == "RUNNING"
    assert ctx.runtime == "01:23:45"
    assert ctx.time_limit == "04:00:00"
    assert ctx.nodelist == "testnode01"
    assert ctx.partition == "gpu"
    assert ctx.user == "alice"
    assert ctx.submit_time == "2026-05-14T08:00:00"
    assert ctx.start_time == "2026-05-14T08:01:00"


def test_probe_slurm_resilient_when_enrichment_commands_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If squeue/scontrol are missing, we still get the job_id from cgroup."""
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    with patch.object(jupyter_mod, "_read_cgroup", return_value=CGROUP_WITH_JOB), \
         patch.object(jupyter_mod, "_run_capture", return_value=None):
        ctx = probe_slurm()
    assert ctx is not None
    assert ctx.job_id == "12345"
    assert ctx.job_name is None
    assert ctx.runtime is None


# ---------------------------------------------------------------------------
# probe_gpu
# ---------------------------------------------------------------------------


def test_probe_gpu_returns_empty_when_no_smi() -> None:
    with patch.object(jupyter_mod, "_run_capture", return_value=None):
        assert probe_gpu() == []


def test_probe_gpu_parses_csv_rows() -> None:
    out = "12345, 35840, GPU-abc123\n67890, 1024, GPU-def456\n"
    with patch.object(jupyter_mod, "_run_capture", return_value=out):
        procs = probe_gpu()
    assert procs == [
        GpuProcess(pid=12345, used_memory="35840 MiB", gpu_uuid="GPU-abc123"),
        GpuProcess(pid=67890, used_memory="1024 MiB", gpu_uuid="GPU-def456"),
    ]


def test_probe_gpu_skips_unparseable_rows() -> None:
    out = "not-a-pid, 1024, GPU-abc\n42, 512, GPU-x\n"
    with patch.object(jupyter_mod, "_run_capture", return_value=out):
        procs = probe_gpu()
    assert len(procs) == 1
    assert procs[0].pid == 42


# ---------------------------------------------------------------------------
# discover_other_servers
# ---------------------------------------------------------------------------


def _server_entry(
    pid: int, port: int, *, host: str = "localhost", token: str = "t"
) -> dict[str, Any]:
    return {
        "pid": pid,
        "port": port,
        "url": f"http://{host}:{port}/",
        "token": token,
        "root_dir": "/home/u",
        "hostname": host,
    }


def test_discover_other_servers_excludes_self(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JPY_PARENT_PID", "111")
    entries = [_server_entry(111, 8888), _server_entry(222, 9999)]
    with patch.object(jupyter_mod, "_list_running_servers", return_value=entries), \
         patch.object(jupyter_mod, "_pid_alive", return_value=True):
        siblings = discover_other_servers()
    assert [s.pid for s in siblings] == [222]
    assert siblings[0].port == 9999


def test_discover_other_servers_prunes_dead_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling on the same host with a dead PID is dropped when
    prune_stale=True."""
    monkeypatch.setenv("JPY_PARENT_PID", "111")
    import socket

    local = socket.gethostname()
    entries = [
        _server_entry(111, 8888, host=local),
        _server_entry(222, 9999, host=local),  # dead
        _server_entry(333, 7777, host="other-node"),  # remote — keep
    ]
    alive = {333}

    def fake_alive(pid: int) -> bool:
        return pid in alive

    with patch.object(jupyter_mod, "_list_running_servers", return_value=entries), \
         patch.object(jupyter_mod, "_pid_alive", side_effect=fake_alive):
        siblings = discover_other_servers(prune_stale=True)
    pids = {s.pid for s in siblings}
    assert pids == {333}


def test_discover_other_servers_keeps_dead_when_prune_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JPY_PARENT_PID", "111")
    entries = [_server_entry(111, 8888), _server_entry(222, 9999)]
    with patch.object(jupyter_mod, "_list_running_servers", return_value=entries), \
         patch.object(jupyter_mod, "_pid_alive", return_value=False):
        siblings = discover_other_servers(prune_stale=False)
    assert [s.pid for s in siblings] == [222]


# ---------------------------------------------------------------------------
# describe_server
# ---------------------------------------------------------------------------


def test_describe_server_aggregates_sessions_and_kernels() -> None:
    sessions = [
        {"notebook": {"path": "work/a.ipynb"}},
        {"notebook": {"path": "work/b.ipynb"}},
        {"notebook": {"path": "work/a.ipynb"}},  # dup
    ]
    kernels = [
        {
            "id": "k1",
            "name": "python3",
            "execution_state": "idle",
            "last_activity": "2026-05-14T09:00:00Z",
            "connections": 1,
        },
    ]

    def fake_http(url: str, token: str | None, *, timeout: float = 3.0) -> Any:
        if url.endswith("/api/sessions"):
            return sessions
        if url.endswith("/api/kernels"):
            return kernels
        return None

    with patch.object(jupyter_mod, "_http_get_json", side_effect=fake_http):
        summary = describe_server("http://localhost:8888/", "tok")
    assert summary["attached_notebooks"] == ["work/a.ipynb", "work/b.ipynb"]
    assert summary["kernels"][0]["id"] == "k1"


def test_describe_server_returns_empty_when_unreachable() -> None:
    with patch.object(jupyter_mod, "_http_get_json", return_value=None):
        summary = describe_server("http://nope/", None)
    assert summary == {"attached_notebooks": [], "kernels": []}


# ---------------------------------------------------------------------------
# init() composition
# ---------------------------------------------------------------------------


def test_init_outside_slurm_outside_jupyter_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Laptop with no kernel, no SLURM: still returns a valid SessionInfo."""
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("JPY_PARENT_PID", raising=False)
    with patch.object(jupyter_mod, "_read_cgroup", return_value=None), \
         patch.object(jupyter_mod, "_list_running_servers", return_value=[]), \
         patch.object(jupyter_mod, "probe_gpu", return_value=[]), \
         patch.object(jupyter_mod, "_current_kernel_id", return_value=None):
        info = init()
    assert isinstance(info, SessionInfo)
    assert info.slurm is None
    assert info.jupyter_url is None
    assert info.cluster_siblings == []
    assert info.gpu_processes == []
    assert info.hostname  # non-empty
    assert isinstance(info.introspected_at, datetime)


def test_init_inside_jupyter_inside_slurm_full_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JPY_PARENT_PID", "111")
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    current = _server_entry(111, 3618, host="testnode01")
    sibling = _server_entry(222, 8562, host="testnode01")

    def fake_http(url: str, token: str | None, *, timeout: float = 3.0) -> Any:
        if url.endswith("/api/sessions"):
            return [{"notebook": {"path": "exp/train.ipynb"}}]
        if url.endswith("/api/kernels"):
            return []
        return None

    def fake_run(argv: list[str], timeout: float = 5.0) -> str | None:
        return None  # squeue/scontrol absent in test

    with patch.object(jupyter_mod, "_read_cgroup", return_value=CGROUP_WITH_JOB), \
         patch.object(jupyter_mod, "_list_running_servers", return_value=[current, sibling]), \
         patch.object(jupyter_mod, "_pid_alive", return_value=True), \
         patch.object(jupyter_mod, "_run_capture", side_effect=fake_run), \
         patch.object(jupyter_mod, "_http_get_json", side_effect=fake_http), \
         patch.object(jupyter_mod, "_current_kernel_id", return_value="kernel-uuid"):
        info = init()
    assert info.jupyter_pid == 111
    assert info.jupyter_port == 3618
    assert info.jupyter_url == "http://testnode01:3618/"
    assert info.slurm is not None
    assert info.slurm.job_id == "12345"
    assert info.kernel_id == "kernel-uuid"
    assert info.attached_notebooks == ["exp/train.ipynb"]
    assert [s.port for s in info.cluster_siblings] == [8562]


def test_whoami_is_alias_for_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JPY_PARENT_PID", raising=False)
    with patch.object(jupyter_mod, "_read_cgroup", return_value=None), \
         patch.object(jupyter_mod, "_list_running_servers", return_value=[]), \
         patch.object(jupyter_mod, "probe_gpu", return_value=[]), \
         patch.object(jupyter_mod, "_current_kernel_id", return_value=None):
        a = whoami()
        b = init()
    # Same shape, both valid
    assert a.hostname == b.hostname


def test_session_info_round_trips_through_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init().model_dump_json()`` → load → equivalent dict. The MCP
    dispatch path relies on this."""
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("JPY_PARENT_PID", raising=False)
    with patch.object(jupyter_mod, "_read_cgroup", return_value=None), \
         patch.object(jupyter_mod, "_list_running_servers", return_value=[]), \
         patch.object(jupyter_mod, "probe_gpu", return_value=[]), \
         patch.object(jupyter_mod, "_current_kernel_id", return_value=None):
        info = init()
    raw = info.model_dump_json()
    loaded = json.loads(raw)
    assert loaded["hostname"] == info.hostname
    assert loaded["slurm"] is None
    # Round-trip through model
    rebuilt = SessionInfo.model_validate(loaded)
    assert rebuilt.hostname == info.hostname


# ---------------------------------------------------------------------------
# pydantic schema sanity
# ---------------------------------------------------------------------------


def test_sibling_session_accepts_minimal_dict() -> None:
    s = SiblingSession(url="http://x/")
    assert s.url == "http://x/"
    assert s.port is None
    assert s.token is None


def test_slurm_context_requires_job_id() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SlurmContext()  # type: ignore[call-arg]
