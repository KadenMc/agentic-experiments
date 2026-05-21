"""Tests for the MCP server tool wrappers.

Each MCP tool is a thin wrapper around the canonical Python API — the
core behavior is already covered by test_runs / test_linking / etc. These
tests verify the tools return JSON-serializable dicts with the expected
shape (the whole point of MCP over CLI: typed structured returns).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from aexp.install import install_scaffold  # noqa: E402


def _git_commit(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture_output=True
    )
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def installed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_commit(repo)
    install_scaffold(repo)
    monkeypatch.chdir(repo)
    return repo


# ---------------------------------------------------------------------------
# Module import
# ---------------------------------------------------------------------------


def test_mcp_server_module_imports() -> None:
    """Bare import must succeed when the [mcp] extra is installed."""
    import aexp.mcp_server as mod

    assert mod.mcp.name == "aexp"


def test_mcp_server_registers_expected_tools() -> None:
    """Every planned tool must be decorated and discoverable on the server."""
    import anyio

    from aexp.mcp_server import mcp

    tool_list = anyio.run(mcp.list_tools)
    names = {t.name for t in tool_list}
    expected = {
        "new_run",
        "list_runs",
        "list_batches",
        "show_run",
        "show_batch",
        "link_run",
        "bind_tracker",
        "validate",
        "sync_offline",
    }
    assert expected.issubset(names), (expected - names, names)


# ---------------------------------------------------------------------------
# Schema introspection — FastMCP auto-generates JSON Schema from type hints.
# Verify the generated schemas have the right required/optional split so Claude
# Code gets an accurate tool catalog.
# ---------------------------------------------------------------------------


def _tools_by_name() -> dict:
    import anyio

    from aexp.mcp_server import mcp

    return {t.name: t for t in anyio.run(mcp.list_tools)}


def test_mcp_tools_have_nonempty_descriptions() -> None:
    """Every tool needs a real description — Claude uses them to choose when to call."""
    for name, tool in _tools_by_name().items():
        assert tool.description, f"tool {name!r} has no description"
        assert len(tool.description.strip()) > 20, f"tool {name!r} description is too short"


@pytest.mark.parametrize(
    "tool_name, required_fields",
    [
        ("new_run", {"experiment_id"}),
        ("show_run", {"job_id"}),
        ("link_run", {"job_id", "experiment_id"}),
        ("show_batch", {"experiment_id"}),
        ("bind_tracker", {"job_id"}),
    ],
)
def test_mcp_tool_required_fields(tool_name: str, required_fields: set[str]) -> None:
    """Each tool's input schema must list exactly the expected required fields."""
    tool = _tools_by_name()[tool_name]
    schema = tool.inputSchema or {}
    got_required = set(schema.get("required") or [])
    assert got_required == required_fields, (
        f"{tool_name}: expected required={required_fields} got {got_required}"
    )


@pytest.mark.parametrize(
    "tool_name",
    ["list_runs", "list_batches", "validate", "sync_offline"],
)
def test_mcp_tools_with_all_optional_fields_have_no_required(tool_name: str) -> None:
    """Zero-arg / all-optional tools must have an empty or missing ``required`` list."""
    tool = _tools_by_name()[tool_name]
    schema = tool.inputSchema or {}
    assert not (schema.get("required") or [])


def test_mcp_tool_schemas_declare_expected_properties() -> None:
    """Sanity-check that known fields appear in each tool's schema properties."""
    tools = _tools_by_name()

    new_run_props = set((tools["new_run"].inputSchema or {}).get("properties", {}).keys())
    # All of these must be declared (required or optional) so callers can pass them.
    assert {
        "experiment_id",
        "hypothesis_id",
        "sub_hypothesis_id",
        "statepoint",
        "experiment_path",
        "include_commit",
    }.issubset(new_run_props)

    bind_props = set((tools["bind_tracker"].inputSchema or {}).get("properties", {}).keys())
    assert {"job_id", "backend", "project", "offline"}.issubset(bind_props)

    validate_props = set((tools["validate"].inputSchema or {}).get("properties", {}).keys())
    assert "mode" in validate_props


# ---------------------------------------------------------------------------
# Tool return shape
# ---------------------------------------------------------------------------


def test_new_run_tool_returns_jsonable_dict(installed_repo: Path) -> None:
    from aexp.mcp_server import new_run

    result = new_run(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"condition": "smoke", "seed": 0},
    )
    # Round-trip through json to prove it's serializable.
    round_tripped = json.loads(json.dumps(result))
    assert round_tripped["experiment_id"] == "E001"
    assert round_tripped["hypothesis_id"] == "H001"
    assert len(round_tripped["job_id"]) == 32
    assert round_tripped["sp"]["condition"] == "smoke"
    assert round_tripped["status"] == "created"


def test_list_runs_tool_returns_list_of_dicts(installed_repo: Path) -> None:
    from aexp.mcp_server import list_runs, new_run

    new_run(experiment_id="E001", statepoint={"c": "f", "seed": 0})
    new_run(experiment_id="E001", statepoint={"c": "f", "seed": 1})
    result = list_runs(experiment_id="E001")
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(r["experiment_id"] == "E001" for r in result)


def test_validate_tool_returns_structured_dict(installed_repo: Path) -> None:
    from aexp.mcp_server import validate

    result = validate()
    assert set(result.keys()) == {"ok", "errors", "warnings"}
    assert isinstance(result["ok"], bool)
    assert isinstance(result["errors"], list)


def test_validate_tool_flags_broken_citation(installed_repo: Path) -> None:
    from aexp.mcp_server import new_run, validate

    # Point at a non-existent experiment → validate should flag it.
    new_run(experiment_id="E999", statepoint={"c": "f"})
    result = validate(mode="runs-only")
    assert not result["ok"]
    codes = [e["code"] for e in result["errors"]]
    assert "run.broken_experiment_link" in codes


def test_bind_tracker_noop_tool_returns_binding(installed_repo: Path) -> None:
    from aexp.mcp_server import bind_tracker, new_run

    created = new_run(experiment_id="E001", statepoint={"c": "f"})
    result = bind_tracker(job_id=created["job_id"], backend="noop")
    assert result["backend"] == "noop"
    assert result["run_id"]
    assert result["job_id"] == created["job_id"]


def test_bind_tracker_rejects_wandb_without_project(installed_repo: Path) -> None:
    from aexp.mcp_server import bind_tracker, new_run

    created = new_run(experiment_id="E001", statepoint={"c": "f"})
    result = bind_tracker(job_id=created["job_id"], backend="wandb")
    assert "error" in result
    assert result["code"] == "missing_project"


# ---------------------------------------------------------------------------
# Protocol-layer round-trip: spawn the server as a subprocess and speak MCP
# over stdio via the official client SDK. Proves the full JSON-RPC path works,
# not just in-process function calls.
# ---------------------------------------------------------------------------


def _run_mcp_client(installed_repo: Path, tool_call: tuple[str, dict] | None = None) -> dict:
    """Spawn ``python -m aexp.mcp_server`` and perform a minimal session.

    Returns a dict with ``initialized``, ``tools``, and optionally ``tool_result``.
    """
    import asyncio
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "aexp.mcp_server"],
        env=None,  # inherit parent env; installed_repo is the cwd
        cwd=str(installed_repo),
    )

    async def run() -> dict:
        out: dict = {}
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                out["server_name"] = init_result.serverInfo.name
                out["initialized"] = True
                tools_resp = await session.list_tools()
                out["tools"] = sorted(t.name for t in tools_resp.tools)
                if tool_call is not None:
                    name, args = tool_call
                    result = await session.call_tool(name, arguments=args)
                    out["tool_result"] = {
                        "is_error": bool(getattr(result, "isError", False)),
                        "content_types": [c.type for c in result.content],
                        "content_snippets": [
                            getattr(c, "text", "")[:200] for c in result.content if hasattr(c, "text")
                        ],
                    }
        return out

    return asyncio.run(run())


def test_mcp_subprocess_initialize_and_list_tools(installed_repo: Path) -> None:
    """Spawning the server as a subprocess: initialize handshake + list_tools
    must produce the full 9-tool catalog.
    """
    out = _run_mcp_client(installed_repo)
    assert out["server_name"] == "aexp"
    assert out["initialized"] is True
    expected = {
        "new_run",
        "list_runs",
        "list_batches",
        "show_run",
        "show_batch",
        "link_run",
        "bind_tracker",
        "validate",
        "sync_offline",
    }
    assert expected.issubset(set(out["tools"])), (expected - set(out["tools"]), out["tools"])


def test_mcp_subprocess_call_tool_validate(installed_repo: Path) -> None:
    """End-to-end: spawn server → call ``validate`` → get a structured result.

    This is the test that would catch protocol-framing bugs, JSON-RPC
    serialization drift, or tool-function crashes that the in-process tests
    would miss.
    """
    out = _run_mcp_client(installed_repo, tool_call=("validate", {}))
    assert "tool_result" in out
    tr = out["tool_result"]
    assert tr["is_error"] is False, tr
    # Result content should include at least one text block mentioning "ok" or "errors".
    joined = "\n".join(tr["content_snippets"]).lower()
    assert "ok" in joined or "errors" in joined, tr


def test_mcp_subprocess_call_tool_error_surfaces_cleanly(
    installed_repo: Path,
) -> None:
    """Calling ``show_run`` with a non-existent job id must not crash the server —
    FastMCP should wrap the exception as an MCP error response (``isError=True``),
    and the subprocess must remain alive enough to complete the roundtrip.
    """
    out = _run_mcp_client(
        installed_repo, tool_call=("show_run", {"job_id": "0" * 32})
    )
    assert out["initialized"] is True
    tr = out["tool_result"]
    # FastMCP wraps tool exceptions into an error response rather than crashing
    # the server. Either an is_error result or a text content mentioning the
    # error is acceptable — the critical thing is the session completed.
    joined = "\n".join(tr["content_snippets"]).lower()
    assert tr["is_error"] is True or "not found" in joined or "runnotfound" in joined, tr
