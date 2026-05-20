"""Tests for the aexp.airgapped relay surface (SSH transport).

The relay runs whitelisted git/wandb commands on an internet-having HPC
login node over SSH, on behalf of an agent whose compute node is
network-isolated.

What's tested here:
- ``python -m aexp.airgapped`` entry point stays wired
- ALLOWED whitelist content
- validate_request happy + error paths (incl. shell-injection guard)
- remote-command construction + shlex quoting
- request(): ssh argv shape, success, non-zero git rc, ssh-transport
  failure, timeout, missing ssh binary, consent gating, missing config
- the laptop-side audit log
- RelayClient construction + method dispatch
- the airgapped CLI (help + the wandb-sync consent gate)

No test makes a real SSH connection -- ``subprocess.run`` is mocked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from aexp.airgapped import (
    ALLOWED,
    RelayClient,
    RelayDownError,
    RelayRejectedError,
    RelayResult,
    RelayTimeoutError,
    RelayValidationError,
    check_connection,
    request,
    validate_request,
)
from aexp.airgapped._relay import _build_remote_command, airgapped_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """A stand-in for what subprocess.run returns."""
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# CLI entry point — `python -m aexp.airgapped` must work
# ---------------------------------------------------------------------------


def test_python_m_aexp_airgapped_has_entry_point() -> None:
    """``python -m aexp.airgapped`` must reach the CLI.

    Regression guard: packages need an explicit ``__main__.py`` for the
    ``python -m`` invocation. This pins that ``aexp/airgapped/__main__.py``
    stays in place and exposes a callable ``main``.
    """
    import importlib

    mod = importlib.import_module("aexp.airgapped.__main__")
    assert callable(getattr(mod, "main", None)), (
        "aexp.airgapped.__main__ must expose a callable `main` "
        "imported from aexp.airgapped._relay"
    )


# ---------------------------------------------------------------------------
# ALLOWED whitelist sanity
# ---------------------------------------------------------------------------


def test_allowed_contains_git_ops() -> None:
    assert "git_pull" in ALLOWED
    assert "git_push" in ALLOWED
    assert "git_fetch" in ALLOWED
    assert "git_status" in ALLOWED
    assert "git_rebase" in ALLOWED


def test_allowed_contains_wandb_sync_with_consent() -> None:
    assert "wandb_sync" in ALLOWED
    assert ALLOWED["wandb_sync"].consent is True


def test_allowed_git_ops_are_auto_approved() -> None:
    for op in ("git_pull", "git_push", "git_fetch", "git_status", "git_rebase"):
        assert ALLOWED[op].consent is False, f"{op} should be auto-approved"


def test_allowed_git_push_has_args_regex() -> None:
    spec = ALLOWED["git_push"]
    assert spec.args_regex is not None
    import re

    assert re.fullmatch(spec.args_regex, "origin")
    assert re.fullmatch(spec.args_regex, "main")
    assert re.fullmatch(spec.args_regex, "feature/foo-bar.baz")
    # Shell-injection chars must NOT match.
    assert not re.fullmatch(spec.args_regex, "main; rm -rf /")
    assert not re.fullmatch(spec.args_regex, "$(whoami)")


# ---------------------------------------------------------------------------
# validate_request — happy paths
# ---------------------------------------------------------------------------


def test_validate_git_pull() -> None:
    op, args = validate_request("git_pull", [])
    assert op == "git_pull"
    assert args == []


def test_validate_git_pull_normalizes_none_args() -> None:
    op, args = validate_request("git_pull", None)
    assert (op, args) == ("git_pull", [])


def test_validate_git_push_with_remote_and_branch() -> None:
    op, args = validate_request("git_push", ["origin", "main"])
    assert op == "git_push"
    assert args == ["origin", "main"]


# ---------------------------------------------------------------------------
# validate_request — error paths
# ---------------------------------------------------------------------------


def test_validate_unknown_op_raises() -> None:
    with pytest.raises(RelayValidationError, match="unknown op"):
        validate_request("rm_rf_slash", [])


def test_validate_git_push_without_args_raises() -> None:
    """F7 in raw form: bare git_push rejected because args_regex is set."""
    with pytest.raises(RelayValidationError, match="requires at least one arg"):
        validate_request("git_push", [])


def test_validate_git_pull_with_args_raises() -> None:
    """Ops without args_regex must have empty args."""
    with pytest.raises(RelayValidationError, match="accepts no per-request args"):
        validate_request("git_pull", ["something"])


def test_validate_too_many_args_raises() -> None:
    with pytest.raises(RelayValidationError, match="too many args"):
        validate_request("git_push", ["x"] * 33)


def test_validate_shell_injection_in_push_arg_raises() -> None:
    """The closed-whitelist invariant: shell metacharacters never get through."""
    with pytest.raises(RelayValidationError, match="failed regex"):
        validate_request("git_push", ["main; rm -rf /"])
    with pytest.raises(RelayValidationError, match="failed regex"):
        validate_request("git_push", ["$(whoami)"])


# ---------------------------------------------------------------------------
# _build_remote_command — quoting
# ---------------------------------------------------------------------------


def test_build_remote_command_basic() -> None:
    cmd = _build_remote_command("/home/me/electricrag", "git_pull", [])
    assert cmd == "cd /home/me/electricrag && git pull --ff-only"


def test_build_remote_command_quotes_repo_with_spaces() -> None:
    cmd = _build_remote_command("/home/me/my repo", "git_pull", [])
    assert cmd == "cd '/home/me/my repo' && git pull --ff-only"


def test_build_remote_command_includes_args() -> None:
    cmd = _build_remote_command("/repo", "git_push", ["origin", "main"])
    assert cmd == "cd /repo && git push origin main"


# ---------------------------------------------------------------------------
# request() — SSH transport
# ---------------------------------------------------------------------------


def test_request_constructs_expected_ssh_argv(tmp_path: Path) -> None:
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0, stdout="ok")
        request(
            "git_pull",
            ssh_host="cluster-login",
            remote_repo="/home/me/electricrag",
            audit_log=tmp_path / "audit.log",
        )
    argv = mock_run.call_args.args[0]
    assert argv[0] == "ssh"
    assert "-n" in argv
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv
    assert argv[-2] == "cluster-login"
    assert argv[-1] == "cd /home/me/electricrag && git pull --ff-only"
    assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_ssh_never_inherits_caller_stdin(tmp_path: Path) -> None:
    """Regression: the relay's ssh must never inherit the caller's stdin.

    ssh launched as `ssh host cmd` keeps reading stdin until EOF. If the
    caller is a long-lived process whose stdin is a never-closing pipe
    (an MCP server's stdio transport is exactly this), ssh stays alive
    after the remote command finishes and the relay call hangs until its
    timeout. Both `request()` and `check_connection()` must pass `-n`
    AND `stdin=subprocess.DEVNULL` so ssh has no stdin to wait on.
    """
    # request()
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0)
        request("git_pull", ssh_host="h", remote_repo="/r",
                audit_log=tmp_path / "audit.log")
        assert "-n" in mock_run.call_args.args[0]
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL

    # check_connection()
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0)
        check_connection(ssh_host="h")
        assert "-n" in mock_run.call_args.args[0]
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_request_returns_relayresult_on_success(tmp_path: Path) -> None:
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0, stdout="Already up to date.")
        result = request(
            "git_pull",
            ssh_host="h",
            remote_repo="/r",
            audit_log=tmp_path / "audit.log",
        )
    assert isinstance(result, RelayResult)
    assert result.op == "git_pull"
    assert result.returncode == 0
    assert "Already up to date." in result.stdout


def test_request_merges_stderr_into_stdout(tmp_path: Path) -> None:
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(
            returncode=0, stdout="out", stderr="progress text"
        )
        result = request(
            "git_fetch", ssh_host="h", remote_repo="/r",
            audit_log=tmp_path / "audit.log",
        )
    assert "out" in result.stdout
    assert "progress text" in result.stdout


def test_request_nonzero_git_rc_is_not_raised(tmp_path: Path) -> None:
    """A non-zero git exit code is a completed round-trip, returned not raised."""
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(
            returncode=1, stdout="", stderr="CONFLICT (content): merge conflict"
        )
        result = request(
            "git_rebase", ssh_host="h", remote_repo="/r",
            audit_log=tmp_path / "audit.log",
        )
    assert result.returncode == 1
    assert "CONFLICT" in result.stdout


def test_request_ssh_rc_255_raises_relaydownerror(tmp_path: Path) -> None:
    """ssh(1) exits 255 only for its own transport failures."""
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(
            returncode=255, stderr="ssh: connect to host h port 22: Connection refused"
        )
        with pytest.raises(RelayDownError, match="SSH transport failure"):
            request(
                "git_pull", ssh_host="h", remote_repo="/r",
                audit_log=tmp_path / "audit.log",
            )


def test_request_git_rc_with_connection_text_is_returned(tmp_path: Path) -> None:
    """A non-255 rc is git's own result even if stderr mentions a connection.

    e.g. the login node's git failing to reach GitHub exits 128, not 255 --
    that is a git failure to surface in the RelayResult, not a relay-down.
    """
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(
            returncode=128,
            stderr="fatal: Could not read from remote repository / Connection refused",
        )
        result = request(
            "git_push", ["origin", "main"], ssh_host="h", remote_repo="/r",
            audit_log=tmp_path / "audit.log",
        )
    assert result.returncode == 128
    assert "Could not read from remote" in result.stdout


def test_request_timeout_raises_relaytimeouterror(tmp_path: Path) -> None:
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=60)
        with pytest.raises(RelayTimeoutError, match="did not finish"):
            request(
                "git_pull", ssh_host="h", remote_repo="/r", timeout=60,
                audit_log=tmp_path / "audit.log",
            )


def test_request_missing_ssh_binary_raises_relaydownerror(tmp_path: Path) -> None:
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError()
        with pytest.raises(RelayDownError, match="ssh.*not found"):
            request(
                "git_pull", ssh_host="h", remote_repo="/r",
                audit_log=tmp_path / "audit.log",
            )


def test_request_wandb_sync_without_approve_raises(tmp_path: Path) -> None:
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        with pytest.raises(RelayRejectedError, match="consent-required"):
            request(
                "wandb_sync", ssh_host="h", remote_repo="/r",
                audit_log=tmp_path / "audit.log",
            )
        mock_run.assert_not_called()


def test_request_wandb_sync_with_approve_runs(tmp_path: Path) -> None:
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0, stdout="synced")
        result = request(
            "wandb_sync", approve=True, ssh_host="h", remote_repo="/r",
            audit_log=tmp_path / "audit.log",
        )
    mock_run.assert_called_once()
    assert result.returncode == 0


def test_request_missing_ssh_host_raises_validationerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AEXP_RELAY_SSH_HOST", raising=False)
    monkeypatch.delenv("AEXP_RELAY_REMOTE_REPO", raising=False)
    with pytest.raises(RelayValidationError, match="ssh_host is required"):
        request("git_pull", remote_repo="/r", audit_log=tmp_path / "audit.log")


def test_request_missing_remote_repo_raises_validationerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AEXP_RELAY_REMOTE_REPO", raising=False)
    with pytest.raises(RelayValidationError, match="remote_repo is required"):
        request("git_pull", ssh_host="h", audit_log=tmp_path / "audit.log")


def test_request_reads_config_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AEXP_RELAY_SSH_HOST", "env-host")
    monkeypatch.setenv("AEXP_RELAY_REMOTE_REPO", "/env/repo")
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0)
        request("git_pull", audit_log=tmp_path / "audit.log")
    argv = mock_run.call_args.args[0]
    assert argv[-2] == "env-host"
    assert argv[-1].startswith("cd /env/repo &&")


def test_request_writes_audit_log(tmp_path: Path) -> None:
    audit = tmp_path / "audit.log"
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0, stdout="ok")
        request("git_pull", ssh_host="h", remote_repo="/r", audit_log=audit)
    assert audit.exists()
    line = audit.read_text(encoding="utf-8").strip()
    assert "op=git_pull" in line
    assert "rc=0" in line


# ---------------------------------------------------------------------------
# RelayClient construction
# ---------------------------------------------------------------------------


def test_relayclient_stores_ssh_host_and_remote_repo() -> None:
    c = RelayClient(ssh_host="cluster-login", remote_repo="/home/me/electricrag")
    assert c.ssh_host == "cluster-login"
    assert c.remote_repo == "/home/me/electricrag"


def test_relayclient_default_timeout_is_60s() -> None:
    assert RelayClient().default_timeout == 60.0


def test_relayclient_default_connect_timeout_is_10s() -> None:
    assert RelayClient().default_timeout  # touch attr
    assert RelayClient().connect_timeout == 10.0


def test_relayclient_override_timeout() -> None:
    assert RelayClient(default_timeout=300.0).default_timeout == 300.0


# ---------------------------------------------------------------------------
# RelayClient method dispatch (with mocked request)
# ---------------------------------------------------------------------------


def _mock_result() -> RelayResult:
    return RelayResult(
        request_id="fake-id", op="fake-op", returncode=0,
        stdout="mock output", duration_s=0.1,
    )


def test_pull_calls_request_with_git_pull() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r").pull()
        mock_req.assert_called_once()
        assert mock_req.call_args.args == ("git_pull",)
        kw = mock_req.call_args.kwargs
        assert kw["args"] is None
        assert kw["ssh_host"] == "h"
        assert kw["remote_repo"] == "/r"


def test_push_default_is_origin_HEAD() -> None:
    """F7/F8 designed out: .push() with no args builds 'git push origin HEAD'."""
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r").push()
        assert mock_req.call_args.args == ("git_push",)
        assert mock_req.call_args.kwargs["args"] == ["origin", "HEAD"]


def test_push_with_branch_and_remote() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r").push(
            branch="feature/foo", remote="myremote"
        )
        assert mock_req.call_args.kwargs["args"] == ["myremote", "feature/foo"]


def test_status_calls_request_with_git_status() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r").status()
        assert mock_req.call_args.args == ("git_status",)


def test_fetch_calls_request_with_git_fetch() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r").fetch()
        assert mock_req.call_args.args == ("git_fetch",)


def test_rebase_calls_request_with_git_rebase() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r").rebase()
        assert mock_req.call_args.args == ("git_rebase",)


def test_request_escape_hatch_passes_op_args_approve() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r").request(
            "wandb_sync", approve=True, timeout=300.0
        )
        assert mock_req.call_args.args == ("wandb_sync",)
        kw = mock_req.call_args.kwargs
        assert kw["approve"] is True
        assert kw["timeout"] == 300.0


def test_timeout_default_falls_through_to_client_default() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r", default_timeout=42.0).pull()
        assert mock_req.call_args.kwargs["timeout"] == 42.0


def test_timeout_per_call_override_wins() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _mock_result()
        RelayClient(ssh_host="h", remote_repo="/r", default_timeout=42.0).pull(
            timeout=99.0
        )
        assert mock_req.call_args.kwargs["timeout"] == 99.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_help_lists_commands() -> None:
    result = CliRunner().invoke(airgapped_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("status", "pull", "push", "fetch", "repo-status", "rebase"):
        assert cmd in result.output


def test_cli_wandb_sync_without_approve_exits_2() -> None:
    """The consent gate: wandb-sync refuses (exit 2) without --approve."""
    result = CliRunner().invoke(airgapped_app, ["wandb-sync"])
    assert result.exit_code == 2
    assert "consent-required" in result.output


# ---------------------------------------------------------------------------
# init_mcp_config / `aexp airgapped init`
# ---------------------------------------------------------------------------

import json  # noqa: E402  -- used by the init tests below

from aexp.airgapped._relay import init_mcp_config  # noqa: E402


def _write_mcp_json(path: Path, *, aexp_env: dict | None = None) -> None:
    """Helper: write a minimal .mcp.json with an aexp server entry."""
    cfg = {
        "mcpServers": {
            "aexp": {
                "command": "python",
                "args": ["-m", "aexp.mcp_server"],
                "env": aexp_env if aexp_env is not None else {"PYTHONUNBUFFERED": "1"},
            }
        }
    }
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def test_init_mcp_config_adds_env_keys(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    _write_mcp_json(cfg)
    result = init_mcp_config(cfg, ssh_host="h4h", remote_repo="/r")
    assert result["already_correct"] is False
    assert len(result["changes"]) == 2
    data = json.loads(cfg.read_text(encoding="utf-8"))
    env = data["mcpServers"]["aexp"]["env"]
    assert env["AEXP_RELAY_SSH_HOST"] == "h4h"
    assert env["AEXP_RELAY_REMOTE_REPO"] == "/r"
    # Pre-existing env keys preserved.
    assert env["PYTHONUNBUFFERED"] == "1"


def test_init_mcp_config_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    _write_mcp_json(cfg)
    init_mcp_config(cfg, ssh_host="h4h", remote_repo="/r")
    result = init_mcp_config(cfg, ssh_host="h4h", remote_repo="/r")
    assert result["already_correct"] is True
    assert result["changes"] == []


def test_init_mcp_config_conflict_without_force_raises(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    _write_mcp_json(cfg, aexp_env={"AEXP_RELAY_SSH_HOST": "old-host"})
    with pytest.raises(RuntimeError, match="already set to 'old-host'"):
        init_mcp_config(cfg, ssh_host="new-host", remote_repo="/r")


def test_init_mcp_config_conflict_with_force_overwrites(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    _write_mcp_json(cfg, aexp_env={"AEXP_RELAY_SSH_HOST": "old-host"})
    result = init_mcp_config(cfg, ssh_host="new-host", remote_repo="/r", force=True)
    assert any("updated AEXP_RELAY_SSH_HOST" in c for c in result["changes"])
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["aexp"]["env"]["AEXP_RELAY_SSH_HOST"] == "new-host"


def test_init_mcp_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        init_mcp_config(tmp_path / "nope.json", ssh_host="h", remote_repo="/r")


def test_init_mcp_config_missing_aexp_server_raises(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="mcpServers.'aexp' not found"):
        init_mcp_config(cfg, ssh_host="h", remote_repo="/r")


def test_init_mcp_config_invalid_json_raises(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    cfg.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        init_mcp_config(cfg, ssh_host="h", remote_repo="/r")


def test_cli_init_writes_env_and_prints_next_steps(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    _write_mcp_json(cfg)
    result = CliRunner().invoke(
        airgapped_app,
        ["init", "--ssh-host", "h4h", "--remote-repo", "/r",
         "--mcp-config", str(cfg)],
    )
    assert result.exit_code == 0
    assert "Host h4h" in result.output
    assert "passwordless SSH" in result.output
    assert "/mcp" in result.output
    assert "aexp airgapped status" in result.output
    # Step 2's inline key-setup recipe must be present so a Windows user
    # never has to leave the CLI output to figure it out.
    assert "ssh-keygen -t ed25519" in result.output
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["aexp"]["env"]["AEXP_RELAY_SSH_HOST"] == "h4h"


def test_cli_init_idempotent_run_is_concise(tmp_path: Path) -> None:
    """A re-run that does nothing should not dump the full setup checklist."""
    cfg = tmp_path / ".mcp.json"
    _write_mcp_json(
        cfg,
        aexp_env={
            "AEXP_RELAY_SSH_HOST": "h4h",
            "AEXP_RELAY_REMOTE_REPO": "/r",
        },
    )
    result = CliRunner().invoke(
        airgapped_app,
        ["init", "--ssh-host", "h4h", "--remote-repo", "/r",
         "--mcp-config", str(cfg)],
    )
    assert result.exit_code == 0
    assert "already matches" in result.output
    # The full checklist should NOT print on an idempotent re-run.
    assert "ssh-keygen" not in result.output
    assert "Step 1." not in result.output


def test_cli_init_conflict_without_force_exits_2(tmp_path: Path) -> None:
    cfg = tmp_path / ".mcp.json"
    _write_mcp_json(cfg, aexp_env={"AEXP_RELAY_SSH_HOST": "old"})
    result = CliRunner().invoke(
        airgapped_app,
        ["init", "--ssh-host", "new", "--remote-repo", "/r",
         "--mcp-config", str(cfg)],
    )
    assert result.exit_code == 2


def test_cli_pull_surfaces_relayresult(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AEXP_RELAY_SSH_HOST", "h")
    monkeypatch.setenv("AEXP_RELAY_REMOTE_REPO", "/r")
    with patch("aexp.airgapped._relay.subprocess.run") as mock_run:
        mock_run.return_value = _fake_proc(returncode=0, stdout="Already up to date.")
        result = CliRunner().invoke(
            airgapped_app, ["pull", "--audit-log", str(tmp_path / "audit.log")]
        )
    assert result.exit_code == 0
    assert "Already up to date." in result.output
