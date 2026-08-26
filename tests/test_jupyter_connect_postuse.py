"""Tests for the ``aexp.hooks.jupyter_connect_postuse`` hook."""
from __future__ import annotations

import json
import subprocess
import sys


def _run(payload: dict | None, *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    stdin = json.dumps(payload) if payload is not None else ""
    return subprocess.run(
        [sys.executable, "-m", "aexp.hooks.jupyter_connect_postuse"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_emits_directive_with_url_on_typical_payload() -> None:
    """A PostToolUse payload with the standard ``tool_input.jupyter_url`` shape
    surfaces a directive that names the URL and references the recipe."""
    payload = {
        "tool_name": "mcp__jupyter__connect_to_jupyter",
        "tool_input": {"jupyter_url": "http://testnode01:3618/?token=abc"},
        "tool_response": {
            "content": [{"type": "text", "text": "Connected to ws://..."}]
        },
    }
    res = _run(payload)
    assert res.returncode == 0
    assert "http://testnode01:3618" in res.stdout
    assert "aexp.jupyter" in res.stdout
    assert "jupyter_parse_introspection" in res.stdout


def test_unknown_url_falls_back_to_placeholder() -> None:
    """If the payload omits a recognizable URL field, the directive still
    emits with a placeholder rather than crashing."""
    payload = {
        "tool_name": "mcp__jupyter__connect_to_jupyter",
        "tool_input": {"server": "http://x/"},  # not a recognized key
    }
    res = _run(payload)
    assert res.returncode == 0
    assert "unknown URL" in res.stdout


def test_handles_error_response_gracefully() -> None:
    """A failed connect_to_jupyter still triggers the directive — re-init is
    even more important when the connection state is unclear."""
    payload = {
        "tool_name": "mcp__jupyter__connect_to_jupyter",
        "tool_input": {"jupyter_url": "http://x/"},
        "tool_response": {"isError": True, "content": []},
    }
    res = _run(payload)
    assert res.returncode == 0
    assert "appears to have failed" in res.stdout
    assert "aexp.jupyter" in res.stdout


def test_no_input_exits_clean() -> None:
    """Empty stdin must not raise."""
    res = _run(None)
    assert res.returncode == 0


def test_malformed_json_exits_clean() -> None:
    """Garbage stdin must not raise."""
    proc = subprocess.run(
        [sys.executable, "-m", "aexp.hooks.jupyter_connect_postuse"],
        input="not json {{{",
        capture_output=True,
        text=True,
        check=False,
        timeout=10.0,
    )
    assert proc.returncode == 0
