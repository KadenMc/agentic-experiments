"""Tests for the aexp hooks (ported from vendored Limina scripts).

Covers:

- ``aexp.hooks._parse_hook_input.parse_hook_input`` for Write/Edit/MultiEdit payloads.
- ``aexp.hooks.enforce_hef_chain`` blocking + allowing paths.
- ``aexp.hooks.kb_write_guard`` carve-outs + delegation to kb_validate.
- ``aexp.hooks.stop_validate`` full-KB validation.

Strategy: hooks derive the repo root from ``os.getcwd()``. Each test
constructs a project directory (via the ``limina_project`` fixture) and
invokes the hook as ``python -m aexp.hooks.<name>`` with ``cwd`` set to
that directory.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aexp.hooks._parse_hook_input import parse_hook_input


def _run_hook(
    project: Path,
    hook_name: str,
    payload: dict | None,
    python_exe: str,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    """Execute ``python -m aexp.hooks.<hook_name>`` with cwd set to ``project``."""
    stdin = json.dumps(payload) if payload is not None else ""
    return subprocess.run(
        [python_exe, "-m", f"aexp.hooks.{hook_name}"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        cwd=str(project),
    )


# ---------------------------------------------------------------------------
# parse_hook_input
# ---------------------------------------------------------------------------


def test_parse_hook_input_write_payload() -> None:
    """Write payload: file_path + content come through untouched."""
    fp, content = parse_hook_input(
        json.dumps(
            {
                "tool_input": {
                    "file_path": "kb/research/hypotheses/H001-foo.md",
                    "content": "body",
                }
            }
        )
    )
    assert fp.endswith("H001-foo.md")
    assert content == "body"


def test_parse_hook_input_edit_payload_reads_existing(tmp_path: Path) -> None:
    """Edit payload: simulates old_string -> new_string against the real file."""
    target = tmp_path / "note.md"
    target.write_text("hello world", encoding="utf-8")

    fp, content = parse_hook_input(
        json.dumps(
            {
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "world",
                    "new_string": "there",
                }
            }
        )
    )

    assert fp == str(target)
    assert content == "hello there"


def test_parse_hook_input_multiedit_payload_sequential(tmp_path: Path) -> None:
    """MultiEdit payload: applies edits in order."""
    target = tmp_path / "note.md"
    target.write_text("one two three", encoding="utf-8")

    fp, content = parse_hook_input(
        json.dumps(
            {
                "tool_input": {
                    "file_path": str(target),
                    "edits": [
                        {"old_string": "one", "new_string": "uno"},
                        {"old_string": "three", "new_string": "tres"},
                    ],
                }
            }
        )
    )

    assert content == "uno two tres"
    assert fp == str(target)


def test_parse_hook_input_invalid_json_yields_empty() -> None:
    """Invalid JSON: returns empty ``(file_path, content)``."""
    fp, content = parse_hook_input("not json at all")
    assert fp == ""
    assert content == ""


# ---------------------------------------------------------------------------
# enforce_hef_chain
# ---------------------------------------------------------------------------


def test_enforce_hef_allows_non_hef_path(limina_project: Path, python_exe: str) -> None:
    """Writes outside experiment/finding paths are not blocked."""
    payload = {
        "tool_input": {
            "file_path": "kb/research/hypotheses/H001-foo.md",
            "content": "any content",
        }
    }
    r = _run_hook(limina_project, "enforce_hef_chain", payload, python_exe)
    assert r.returncode == 0, (r.returncode, r.stderr)


def test_enforce_hef_blocks_experiment_missing_hypothesis_ref(
    limina_project: Path, python_exe: str
) -> None:
    """Experiment without any hypothesis reference is blocked."""
    payload = {
        "tool_input": {
            "file_path": "kb/research/experiments/E042-bar.md",
            "content": "no reference here",
        }
    }
    r = _run_hook(limina_project, "enforce_hef_chain", payload, python_exe)
    assert r.returncode == 2
    assert "without a hypothesis reference" in r.stderr


def test_enforce_hef_blocks_experiment_referencing_missing_hypothesis(
    limina_project: Path, python_exe: str
) -> None:
    """Experiment with hypothesis ref but no matching hypothesis file is blocked."""
    payload = {
        "tool_input": {
            "file_path": "kb/research/experiments/E001-foo.md",
            "content": "> **Hypothesis**: H999\n",
        }
    }
    r = _run_hook(limina_project, "enforce_hef_chain", payload, python_exe)
    assert r.returncode == 2
    assert "no hypothesis file found" in r.stderr


def test_enforce_hef_allows_experiment_with_existing_hypothesis(
    limina_project: Path, python_exe: str
) -> None:
    """Experiment + valid hypothesis ref + matching hypothesis file -> allowed."""
    (limina_project / "kb" / "research" / "hypotheses").mkdir(parents=True, exist_ok=True)
    (limina_project / "kb" / "research" / "hypotheses" / "H007-smoke.md").write_text(
        "hypothesis body", encoding="utf-8"
    )
    payload = {
        "tool_input": {
            "file_path": "kb/research/experiments/E123-ok.md",
            "content": "> **Hypothesis**: H007\n",
        }
    }
    r = _run_hook(limina_project, "enforce_hef_chain", payload, python_exe)
    assert r.returncode == 0, (r.returncode, r.stderr)


def test_enforce_hef_blocks_finding_missing_experiment_ref(
    limina_project: Path, python_exe: str
) -> None:
    """Finding without any experiment reference is blocked."""
    payload = {
        "tool_input": {
            "file_path": "kb/research/findings/F001-verdict.md",
            "content": "bare finding body",
        }
    }
    r = _run_hook(limina_project, "enforce_hef_chain", payload, python_exe)
    assert r.returncode == 2
    assert "without an experiment reference" in r.stderr


def test_enforce_hef_allows_finding_with_existing_experiment(
    limina_project: Path, python_exe: str
) -> None:
    """Finding + valid experiment ref + matching file -> allowed."""
    (limina_project / "kb" / "research" / "experiments").mkdir(parents=True, exist_ok=True)
    (limina_project / "kb" / "research" / "experiments" / "E042-bar.md").write_text(
        "experiment body", encoding="utf-8"
    )
    payload = {
        "tool_input": {
            "file_path": "kb/research/findings/F001-verdict.md",
            "content": "> **Experiment**: E042\n",
        }
    }
    r = _run_hook(limina_project, "enforce_hef_chain", payload, python_exe)
    assert r.returncode == 0, (r.returncode, r.stderr)


def test_enforce_hef_accepts_frontmatter_hypothesis_key(
    limina_project: Path, python_exe: str
) -> None:
    """YAML frontmatter ``hypothesis: H###`` is picked up alongside blockquote metadata."""
    (limina_project / "kb" / "research" / "hypotheses").mkdir(parents=True, exist_ok=True)
    (limina_project / "kb" / "research" / "hypotheses" / "H003-fm.md").write_text(
        "hypothesis body", encoding="utf-8"
    )
    payload = {
        "tool_input": {
            "file_path": "kb/research/experiments/E004-fm.md",
            "content": "---\nhypothesis: \"H003\"\n---\n\nbody\n",
        }
    }
    r = _run_hook(limina_project, "enforce_hef_chain", payload, python_exe)
    assert r.returncode == 0, (r.returncode, r.stderr)


# ---------------------------------------------------------------------------
# kb_write_guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "some/other/file.md",          # outside kb/
        "kb/research/hypotheses/H001-foo.txt",  # non-md
        "kb/research/data/dataset.md",  # carve-out
        "kb/lessons/learned.md",        # carve-out
        "kb/.hidden/foo.md",            # hidden segment
    ],
)
def test_kb_write_guard_skips_non_guarded_paths(
    limina_project: Path, python_exe: str, path: str
) -> None:
    payload = {"tool_input": {"file_path": path, "content": "any"}}
    r = _run_hook(limina_project, "kb_write_guard", payload, python_exe)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


def test_kb_write_guard_blocks_invalid_md(
    limina_project: Path, python_exe: str
) -> None:
    """A malformed artifact under kb/research/ triggers kb_validate -> blocked."""
    target = limina_project / "kb" / "research" / "hypotheses" / "H050-bogus.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("this is not a valid Limina artifact", encoding="utf-8")

    payload = {
        "tool_input": {
            "file_path": str(target),
            "content": "this is not a valid Limina artifact",
        }
    }
    r = _run_hook(limina_project, "kb_write_guard", payload, python_exe)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "BLOCKED" in r.stderr


# ---------------------------------------------------------------------------
# stop_validate
# ---------------------------------------------------------------------------


def test_stop_validate_passes_on_clean_kb(
    limina_project: Path, python_exe: str
) -> None:
    """Vendored Limina's shipped kb/ template validates cleanly out of the box."""
    r = _run_hook(limina_project, "stop_validate", None, python_exe, timeout=30)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


def test_stop_validate_no_kb_dir_is_noop(
    limina_project: Path, python_exe: str
) -> None:
    """Missing kb/ dir -> hook returns 0 without running the validator."""
    import shutil as _shutil

    _shutil.rmtree(limina_project / "kb")
    r = _run_hook(limina_project, "stop_validate", None, python_exe, timeout=10)
    assert r.returncode == 0


def test_stop_validate_blocks_on_broken_kb(
    limina_project: Path, python_exe: str
) -> None:
    """Introduce a broken artifact -> stop_validate exits 2 with BLOCKED."""
    broken = limina_project / "kb" / "research" / "experiments" / "E999-broken.md"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("no frontmatter, no valid structure", encoding="utf-8")

    r = _run_hook(limina_project, "stop_validate", None, python_exe, timeout=30)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "BLOCKED" in r.stderr
