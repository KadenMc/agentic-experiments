"""Tests for the aexp.airgapped relay surface.

These are port-level tests covering the public API. The exhaustive
daemon-lifecycle + consent-state-machine + GC tests live upstream at
``electricrag/tests/dev/test_relay.py`` (56 tests, all green) and serve
as the executable spec for behaviors not retested here.

What's tested here:
- Public-name imports
- ALLOWED whitelist content (sanity)
- validate_request happy paths + error paths
- Removing the electricrag-specific cwd default (must pass explicit cwd now)
- The AEXP_RELAY_CWD_NAMES env-var allowlist hook
- RelayClient construction defaults + overrides
- RelayClient method dispatch (via monkey-patched request)

What is NOT tested here:
- Full daemon process lifecycle
- Consent state machine (approved/rejected handling)
- Heartbeat staleness recovery
- GC of old outbox / log files
- Stale-processing recovery

For those, see the electricrag test file linked above.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aexp.airgapped import (
    ALLOWED,
    DEFAULT_QUEUE,
    OpSpec,
    RelayClient,
    RelayError,
    RelayResult,
    RelayValidationError,
    request,
    validate_request,
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
    # Common refspec characters should match
    import re
    assert re.fullmatch(spec.args_regex, "origin")
    assert re.fullmatch(spec.args_regex, "main")
    assert re.fullmatch(spec.args_regex, "feature/foo-bar.baz")
    # Shell-injection chars should NOT match
    assert not re.fullmatch(spec.args_regex, "main; rm -rf /")
    assert not re.fullmatch(spec.args_regex, "$(whoami)")


# ---------------------------------------------------------------------------
# validate_request — happy paths
# ---------------------------------------------------------------------------


def test_validate_git_pull_with_home_cwd() -> None:
    op, args, cwd = validate_request(
        {"op": "git_pull", "args": [], "cwd": str(Path.home())}
    )
    assert op == "git_pull"
    assert args == []
    assert cwd == Path.home().resolve()


def test_validate_git_push_with_remote_and_branch(tmp_path: Path) -> None:
    # tmp_path on Windows can be under home; on Linux too. Use a known-home subpath.
    repo = Path.home() / "_aexp_test_repo_for_validate"
    repo.mkdir(exist_ok=True)
    try:
        op, args, cwd = validate_request(
            {"op": "git_push", "args": ["origin", "main"], "cwd": str(repo)}
        )
        assert op == "git_push"
        assert args == ["origin", "main"]
    finally:
        repo.rmdir()


# ---------------------------------------------------------------------------
# validate_request — error paths (the design-out for F7/F8 lives here)
# ---------------------------------------------------------------------------


def test_validate_missing_cwd_raises() -> None:
    """Removed the electricrag-specific default; cwd is now required."""
    with pytest.raises(RelayValidationError, match="cwd is required"):
        validate_request({"op": "git_pull", "args": []})


def test_validate_unknown_op_raises() -> None:
    with pytest.raises(RelayValidationError, match="unknown op"):
        validate_request(
            {"op": "rm_rf_slash", "cwd": str(Path.home())}
        )


def test_validate_git_push_without_args_raises() -> None:
    """The F7 friction in raw form: bare git_push rejected because args_regex is set."""
    with pytest.raises(RelayValidationError, match="requires at least one arg"):
        validate_request(
            {"op": "git_push", "args": [], "cwd": str(Path.home())}
        )


def test_validate_git_pull_with_args_raises() -> None:
    """Ops without args_regex must have empty args."""
    with pytest.raises(RelayValidationError, match="accepts no per-request args"):
        validate_request(
            {"op": "git_pull", "args": ["something"], "cwd": str(Path.home())}
        )


def test_validate_cwd_outside_home_raises() -> None:
    # /tmp (or /var or C:/Windows) is outside home on any platform
    if os.name == "nt":
        outside = "C:/Windows"
    else:
        outside = "/tmp"
    with pytest.raises(RelayValidationError, match="cwd not under home"):
        validate_request(
            {"op": "git_pull", "args": [], "cwd": outside}
        )


def test_validate_shell_injection_in_push_arg_raises() -> None:
    with pytest.raises(RelayValidationError, match="failed regex"):
        validate_request(
            {
                "op": "git_push",
                "args": ["main; rm -rf /"],
                "cwd": str(Path.home()),
            }
        )


# ---------------------------------------------------------------------------
# AEXP_RELAY_CWD_NAMES env-var allowlist
# ---------------------------------------------------------------------------


def test_cwd_allowlist_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without AEXP_RELAY_CWD_NAMES, any subdir of home is allowed."""
    monkeypatch.delenv("AEXP_RELAY_CWD_NAMES", raising=False)
    # Need to reload module to re-read the env var
    import importlib
    import aexp.airgapped._relay as relay_mod
    importlib.reload(relay_mod)
    assert relay_mod._ALLOWED_CWD_NAMES == ()


def test_cwd_allowlist_respects_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEXP_RELAY_CWD_NAMES", "myrepo,other-repo")
    import importlib
    import aexp.airgapped._relay as relay_mod
    importlib.reload(relay_mod)
    assert relay_mod._ALLOWED_CWD_NAMES == ("myrepo", "other-repo")
    # cleanup: reload back to empty
    monkeypatch.delenv("AEXP_RELAY_CWD_NAMES", raising=False)
    importlib.reload(relay_mod)


# ---------------------------------------------------------------------------
# RelayClient construction
# ---------------------------------------------------------------------------


def test_relayclient_default_cwd_is_cwd() -> None:
    c = RelayClient()
    assert c.cwd == Path.cwd()


def test_relayclient_default_queue_is_DEFAULT_QUEUE() -> None:
    c = RelayClient()
    assert c.queue == DEFAULT_QUEUE


def test_relayclient_default_timeout_is_60s() -> None:
    c = RelayClient()
    assert c.default_timeout == 60.0


def test_relayclient_override_cwd() -> None:
    c = RelayClient(cwd=Path.home())
    assert c.cwd == Path.home()


def test_relayclient_override_queue(tmp_path: Path) -> None:
    custom_queue = tmp_path / "custom-relay"
    c = RelayClient(queue=custom_queue)
    assert c.queue == custom_queue


def test_relayclient_override_timeout() -> None:
    c = RelayClient(default_timeout=300.0)
    assert c.default_timeout == 300.0


def test_relayclient_string_cwd_becomes_path() -> None:
    c = RelayClient(cwd=str(Path.home()))
    assert isinstance(c.cwd, Path)
    assert c.cwd == Path.home()


# ---------------------------------------------------------------------------
# RelayClient method dispatch (with mocked request)
# ---------------------------------------------------------------------------


def _make_mock_result() -> RelayResult:
    return RelayResult(
        request_id="fake-id",
        op="fake-op",
        returncode=0,
        stdout="mock output",
        duration_s=0.1,
    )


def test_pull_calls_request_with_git_pull() -> None:
    """F4 designed out: caller just says .pull(); we build args correctly."""
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home())
        c.pull()
        mock_req.assert_called_once()
        call_kwargs = mock_req.call_args.kwargs
        assert mock_req.call_args.args == ("git_pull",)
        assert call_kwargs["args"] is None
        assert call_kwargs["cwd"] == str(Path.home())


def test_push_default_is_origin_HEAD() -> None:
    """F7/F8 designed out: .push() with no args builds 'git push origin HEAD'."""
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home())
        c.push()
        call_kwargs = mock_req.call_args.kwargs
        assert mock_req.call_args.args == ("git_push",)
        assert call_kwargs["args"] == ["origin", "HEAD"]


def test_push_with_branch_and_remote() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home())
        c.push(branch="feature/foo", remote="myremote")
        call_kwargs = mock_req.call_args.kwargs
        assert call_kwargs["args"] == ["myremote", "feature/foo"]


def test_status_calls_request_with_git_status() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home())
        c.status()
        assert mock_req.call_args.args == ("git_status",)


def test_fetch_calls_request_with_git_fetch() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home())
        c.fetch()
        assert mock_req.call_args.args == ("git_fetch",)


def test_rebase_calls_request_with_git_rebase() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home())
        c.rebase()
        assert mock_req.call_args.args == ("git_rebase",)


def test_request_escape_hatch_passes_op_and_args_through() -> None:
    """The .request() method is the escape hatch for ops not exposed as methods."""
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home())
        c.request("wandb_sync", timeout=300.0)
        call_kwargs = mock_req.call_args.kwargs
        assert mock_req.call_args.args == ("wandb_sync",)
        assert call_kwargs["timeout"] == 300.0


def test_timeout_default_falls_through_to_client_default() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home(), default_timeout=42.0)
        c.pull()
        call_kwargs = mock_req.call_args.kwargs
        assert call_kwargs["timeout"] == 42.0


def test_timeout_per_call_override_wins() -> None:
    with patch("aexp.airgapped.client.request") as mock_req:
        mock_req.return_value = _make_mock_result()
        c = RelayClient(cwd=Path.home(), default_timeout=42.0)
        c.pull(timeout=99.0)
        call_kwargs = mock_req.call_args.kwargs
        assert call_kwargs["timeout"] == 99.0
