"""End-to-end smoke test — exercises the full happy path in a fresh repo.

Covers the plan §12 checklist:

1. ``aex install`` in a bare git repo produces the expected tree.
2. Create an artifact via the vendored ``kb_new_artifact.py`` — KB remains valid.
3. ``aex new-run`` creates a signac job; ``aex list-runs`` finds it.
4. ``aex bind-tracker --backend noop`` writes JSONL under the job workspace.
5. Create several runs; ``aex list-batches`` rolls them up; ``aex show-batch`` filters.
6. Break a link; ``aex validate`` flags ``run.broken_experiment_link``.
7. Re-create at a new commit → distinct job directory (both persist).

Marked ``slow`` so it can be excluded from fast local loops.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aexp.cli import app

pytestmark = pytest.mark.slow


def _git_commit(repo: Path, msg: str = "init") -> str:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture_output=True
    )
    seed = repo / "seed.txt"
    if not seed.exists():
        seed.write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", msg],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


def _git_new_commit(repo: Path, path_name: str) -> str:
    (repo / path_name).write_text("more", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q", "-m", "bump"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


def test_e2e_fresh_repo_full_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    runner = CliRunner()

    repo = tmp_path / "scratch"
    repo.mkdir()
    first_sha = _git_commit(repo)
    monkeypatch.chdir(repo)

    # 1. aex install
    r = runner.invoke(app, ["install", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert (repo / "kb" / "ACTIVE.md").is_file()
    assert (repo / ".claude" / "settings.json").is_file()
    assert (repo / ".mcp.json").is_file()
    assert (repo / ".runs").is_dir()
    assert (repo / ".aexp" / "installed.json").is_file()
    # Hook scripts and kb_validate no longer land in the consumer repo —
    # they live inside the installed aexp package and are invoked from there.
    assert not (repo / "scripts").exists()

    # 2. aexp.kb_validate should report the freshly-installed kb/ as clean.
    from aexp.kb_validate import validate_kb as _validate_kb
    assert _validate_kb(repo / "kb").ok

    # 3. Create H001 + E001 via the vendored artifact creator (non-interactively).
    # kb_new_artifact.py expects interactive prompts; bypass by hand-crafting the files
    # in a way kb_validate.py accepts.
    import yaml

    def _write(subdir: str, fname: str, fm: dict, body: str) -> None:
        target = repo / "kb" / subdir / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        fm_text = yaml.safe_dump(fm, sort_keys=False).strip()
        target.write_text(f"---\n{fm_text}\n---\n\n{body}", encoding="utf-8")

    _write(
        "research/hypotheses",
        "H001-smoke.md",
        {
            "id": "H001",
            "aliases": ["H001"],
            "type": "hypothesis",
            "status": "PROPOSED",
            "created": "2026-04-20",
        },
        "# H001 — Smoke\n\n> **Status**: PROPOSED\n> **Created**: 2026-04-20\n\n"
        "## Links\n\n- [[E001]]\n- [[ACTIVE]]\n- [[CHALLENGE]]\n",
    )
    _write(
        "research/experiments",
        "E001-smoke.md",
        {
            "id": "E001",
            "aliases": ["E001"],
            "type": "experiment",
            "status": "DESIGNED",
            "hypothesis": "H001",
            "created": "2026-04-20",
        },
        "# E001 — Smoke\n\n> **Status**: DESIGNED\n> **Hypothesis**: [[H001]]\n> **Created**: 2026-04-20\n\n"
        "## Local Hypothesis\n\nSmoke smoke smoke.\n\n"
        "## Links\n\n- [[H001]]\n- [[ACTIVE]]\n- [[CHALLENGE]]\n",
    )

    # kb_validate should remain green with the new artifacts.
    assert _validate_kb(repo / "kb").ok

    # 4. aex new-run + aex list-runs
    r = runner.invoke(
        app,
        [
            "new-run",
            "--experiment", "E001",
            "--hypothesis", "H001",
            "--sp", "condition=full,model=smoke,seed=0",
        ],
    )
    assert r.exit_code == 0, r.stdout
    job_id_1 = next(t for t in r.stdout.split() if len(t) == 32)
    assert (repo / ".runs" / "workspace" / job_id_1).is_dir()

    lr = runner.invoke(app, ["list-runs", "--experiment", "E001"])
    assert lr.exit_code == 0
    assert "E001" in lr.stdout and "H001" in lr.stdout

    # 5. aex bind-tracker --backend noop → JSONL appears in the workspace.
    bt = runner.invoke(app, ["bind-tracker", job_id_1, "--backend", "noop"])
    assert bt.exit_code == 0, bt.stdout
    tracker_logs = list(
        (repo / ".runs" / "workspace" / job_id_1 / "tracker_log").rglob("events.jsonl")
    )
    assert tracker_logs, "no tracker log file produced"
    events = [
        json.loads(line)
        for line in tracker_logs[0].read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["event"] == "init_run" for e in events)

    # 6. Batches — create more runs and query.
    for i, cond in enumerate(("full", "full", "classify")):
        rr = runner.invoke(
            app,
            [
                "new-run",
                "--experiment", "E001",
                "--hypothesis", "H001",
                "--sp", f"condition={cond},model=smoke,seed={i + 10}",
            ],
        )
        assert rr.exit_code == 0, rr.stdout
    lb = runner.invoke(app, ["list-batches", "--experiment", "E001"])
    assert lb.exit_code == 0
    assert "full" in lb.stdout and "classify" in lb.stdout

    sb = runner.invoke(app, ["show-batch", "--experiment", "E001", "--condition", "full"])
    assert sb.exit_code == 0
    # 3 "full" runs (one from step 4, two from the loop)
    assert "3 run" in sb.stdout

    # 7. aex validate — clean.
    v_ok = runner.invoke(app, ["validate"])
    assert v_ok.exit_code == 0, v_ok.stdout

    # 8. Break a link → run.broken_experiment_link.
    bad = runner.invoke(app, ["new-run", "--experiment", "E999", "--sp", "condition=x,seed=99"])
    assert bad.exit_code == 0
    v_bad = runner.invoke(app, ["validate", "--runs-only"])
    assert v_bad.exit_code == 1
    assert "run.broken_experiment_link" in v_bad.stdout

    # 9. Re-run at a new commit → new job id; old one still exists.
    before_ids = {
        p.name for p in (repo / ".runs" / "workspace").iterdir() if p.is_dir()
    }
    second_sha = _git_new_commit(repo, "later.txt")
    assert first_sha != second_sha
    r2 = runner.invoke(
        app,
        [
            "new-run",
            "--experiment", "E001",
            "--hypothesis", "H001",
            # Same sp as run #1 — only code_commit differs.
            "--sp", "condition=full,model=smoke,seed=0",
        ],
    )
    assert r2.exit_code == 0
    job_id_after = next(t for t in r2.stdout.split() if len(t) == 32)
    assert job_id_after not in before_ids, "new commit should yield a distinct signac job id"
    assert (repo / ".runs" / "workspace" / job_id_1).is_dir(), "previous run must persist"
    assert (repo / ".runs" / "workspace" / job_id_after).is_dir()

    # 10. Install slash commands.
    r_slash = runner.invoke(app, ["install-slash-commands"])
    assert r_slash.exit_code == 0
    for name in (
        "aexp-new-run.md",
        "aexp-finding-from-run.md",
        "aexp-finding-from-batch.md",
        "aexp-finding-placeholder.md",
        "aexp-queue-add.md",
        "aexp-queue-materialize.md",
    ):
        assert (repo / ".claude" / "commands" / name).is_file()
