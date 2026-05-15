"""PostToolUse hook: nudge re-introspection after ``connect_to_jupyter``.

Fires whenever the agent calls any Jupyter MCP server's
``connect_to_jupyter`` tool. Emits a high-salience instruction telling the
agent to immediately re-run :func:`aexp.jupyter.init` via the live Jupyter
MCP. This closes the "re-init blind spot" — an agent that switches ports
mid-session would otherwise carry stale identity beliefs into subsequent
``execute_code`` / ``execute_cell`` calls.

The hook never blocks (always exits 0). It only informs.
"""
from __future__ import annotations

import json
import sys
from typing import Any


def _extract_jupyter_url(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, dict):
        for key in ("jupyter_url", "url", "server_url"):
            val = tool_input.get(key)
            if isinstance(val, str) and val:
                return val
    return "(unknown URL)"


def _summarize_response(payload: dict[str, Any]) -> str:
    """Return a one-line summary of the tool response, or ``""`` if uninformative.

    Defensive — different MCP servers shape their responses differently.
    """
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        if resp.get("isError"):
            return "(connection appears to have failed; the directive still applies)"
        # FastMCP-style: {"content": [{"type": "text", "text": "..."}]}
        content = resp.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                txt = first.get("text")
                if isinstance(txt, str) and txt:
                    snippet = txt.strip().splitlines()[0][:120]
                    return f"(response: {snippet})"
    return ""


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    url = _extract_jupyter_url(payload)
    response_summary = _summarize_response(payload)

    print(
        "[aexp] Jupyter connection changed to "
        f"{url}.\n"
        "\n"
        "You MUST re-establish session identity before executing any code "
        "on this connection. Any prior beliefs about which kernel you are "
        "talking to are now stale.\n"
        "\n"
        "Step 1. Dispatch this on the newly connected Jupyter MCP:\n"
        '    execute_code(code="from aexp.jupyter import init; import json; '
        'print(json.dumps(init().model_dump(), default=str))")\n'
        "\n"
        "Step 2. Pass the stdout to "
        "`mcp__aexp__jupyter_parse_introspection(raw_output=<...>)` to get a "
        "structured SessionInfo.\n"
        "\n"
        "Step 3. Read these fields and state them back to the user, skipping "
        "fields that are null/empty (e.g. no SLURM context outside a cluster, "
        "no GPU on a CPU box) -- do not invent values for what is not there:\n"
        "  - hostname (always populated)\n"
        "  - jupyter_url + jupyter_port (always populated when connected)\n"
        "  - attached_notebooks (only if non-empty)\n"
        "  - slurm.job_id + slurm.job_name + slurm.nodelist (only if slurm is non-null)\n"
        "  - gpu_processes (only if non-empty)\n"
        "\n"
        "Step 4. Ask the user to confirm this is the session they intended. "
        "If anything looks wrong (host you didn't mean, attached notebook "
        "you didn't expect, a busy kernel you don't recognize), STOP and "
        "wait for direction. Otherwise proceed."
    )
    if response_summary:
        print(response_summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
