"""Integration tests for the Typer CLI — drives `aex` verbs against a real repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aexp.cli import app
from aexp.install import install_scaffold


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
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    # Rich truncates by default at 80 columns — force wider so assertions
    # on rendered cell text aren't defeated by ellipsis.
    monkeypatch.setenv("COLUMNS", "200")
    return CliRunner()


@pytest.fixture
def installed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install Limina, chdir into the repo, and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_commit(repo)
    install_scaffold(repo)
    monkeypatch.chdir(repo)
    return repo


# ---------------------------------------------------------------------------
# artifact creation verbs
# ---------------------------------------------------------------------------


def test_new_hypothesis_new_experiment_new_finding_chain(
    runner: CliRunner, installed_repo: Path
) -> None:
    r = runner.invoke(app, ["new-hypothesis", "--title", "my hypothesis"])
    assert r.exit_code == 0, r.output
    assert "H001" in r.output

    r = runner.invoke(
        app, ["new-experiment", "--title", "my experiment", "--hypothesis", "H001"]
    )
    assert r.exit_code == 0, r.output
    assert "E001" in r.output
    assert "backlinks patched" in r.output

    r = runner.invoke(
        app,
        [
            "new-finding",
            "--title",
            "verdict",
            "--hypothesis",
            "H001",
            "--experiment",
            "E001",
            "--impact",
            "HIGH",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "F001" in r.output

    validate = runner.invoke(app, ["validate", "--kb-only"])
    assert validate.exit_code == 0, validate.output


def test_new_experiment_missing_parent_exits_nonzero(
    runner: CliRunner, installed_repo: Path
) -> None:
    r = runner.invoke(
        app, ["new-experiment", "--title", "t", "--hypothesis", "H099"]
    )
    assert r.exit_code != 0


# ---------------------------------------------------------------------------
# version + install
# ---------------------------------------------------------------------------


def test_package_version_matches_pyproject() -> None:
    """Regression guard: ``aexp.__version__`` must agree with ``pyproject.toml``.

    Previously the version was hard-coded in ``src/aexp/__init__.py`` and
    drifted silently when ``pyproject.toml`` was bumped. The current module
    reads from installed package metadata — this test pins that behaviour so
    any future regression (e.g. reverting to a literal string) is caught.
    """
    import tomllib
    from pathlib import Path

    from aexp import __version__

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert __version__ == pyproject["project"]["version"]


def test_cli_version(runner: CliRunner) -> None:
    """``aexp version`` prints whatever the installed package metadata reports."""
    from aexp import __version__

    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_cli_python_dash_m_invocation() -> None:
    """``python -m aexp version`` is the PATH-independent
    invocation path used by shipped slash commands. Must work on any shell
    where ``python`` has the package installed.
    """
    import subprocess
    import sys

    from aexp import __version__

    result = subprocess.run(
        [sys.executable, "-m", "aexp", "version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == __version__


def test_cli_install_fresh_repo(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git_commit(repo)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["install", "--yes"])
    assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
    assert (repo / "kb" / "ACTIVE.md").is_file()
    assert (repo / ".runs").is_dir()


def test_cli_install_dry_run_writes_nothing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run prints the plan but leaves the repo untouched."""
    repo = tmp_path / "dry"
    repo.mkdir()
    _git_commit(repo)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["install", "--dry-run"])
    assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
    assert "dry-run plan" in result.stdout
    # Nothing landed.
    assert not (repo / "kb").exists()
    assert not (repo / ".claude").exists()
    assert not (repo / ".mcp.json").exists()
    assert not (repo / ".aexp").exists()


def test_cli_install_dev_flag_writes_current_interpreter_to_mcp_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dev writes a direct-Python .mcp.json for live editable-install dev."""
    import json
    import sys

    repo = tmp_path / "dev"
    repo.mkdir()
    _git_commit(repo)
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["install", "--yes", "--dev"])
    assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
    # Heads-up mentions dev mode so the user knows .mcp.json is machine-specific.
    assert "Dev mode" in result.stdout
    mcp = json.loads((repo / ".mcp.json").read_text("utf-8"))
    entry = mcp["mcpServers"]["aexp"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "aexp.mcp_server"]


def test_cli_install_no_tty_without_yes_aborts(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-interactive run without --yes or --dry-run aborts with exit code 1."""
    repo = tmp_path / "abort"
    repo.mkdir()
    _git_commit(repo)
    monkeypatch.chdir(repo)
    # CliRunner defaults to non-tty stdin; no --yes, no --dry-run.
    result = runner.invoke(app, ["install"])
    assert result.exit_code == 1
    assert "rerun with --yes" in result.stdout or "Aborted" in result.stdout


# ---------------------------------------------------------------------------
# new-run + list-runs + show-run
# ---------------------------------------------------------------------------


def test_cli_new_run_then_list(runner: CliRunner, installed_repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "new-run",
            "--experiment", "E001",
            "--hypothesis", "H001",
            "--sp", "condition=full,model=smoke",
        ],
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "created" in result.stdout.lower()

    lst = runner.invoke(app, ["list-runs", "--experiment", "E001"])
    assert lst.exit_code == 0
    assert "E001" in lst.stdout
    assert "H001" in lst.stdout
    assert "full" in lst.stdout


def test_cli_list_runs_empty_is_ok(runner: CliRunner, installed_repo: Path) -> None:
    result = runner.invoke(app, ["list-runs"])
    assert result.exit_code == 0
    assert "runs (0)" in result.stdout


def test_cli_show_run_renders(runner: CliRunner, installed_repo: Path) -> None:
    new = runner.invoke(
        app,
        ["new-run", "--experiment", "E001", "--sp", "condition=full"],
    )
    assert new.exit_code == 0
    # Pull the short id out of the CLI output.
    short = None
    for token in new.stdout.split():
        if len(token) == 32:  # full id in some layouts; we printed full id
            short = token
            break
    assert short is not None, new.stdout
    result = runner.invoke(app, ["show-run", short])
    assert result.exit_code == 0
    assert "state_point" in result.stdout
    assert "condition" in result.stdout


# ---------------------------------------------------------------------------
# list-batches + show-batch
# ---------------------------------------------------------------------------


def test_cli_list_and_show_batches(runner: CliRunner, installed_repo: Path) -> None:
    for i, cond in enumerate(("full", "full", "classify")):
        r = runner.invoke(
            app,
            [
                "new-run",
                "--experiment", "E001",
                "--hypothesis", "H001",
                "--sp", f"condition={cond},seed={i}",
            ],
        )
        assert r.exit_code == 0, r.stdout
    batches = runner.invoke(app, ["list-batches", "--experiment", "E001"])
    assert batches.exit_code == 0
    assert "full" in batches.stdout
    assert "classify" in batches.stdout

    full_batch = runner.invoke(
        app, ["show-batch", "--experiment", "E001", "--condition", "full"]
    )
    assert full_batch.exit_code == 0
    assert "2 run" in full_batch.stdout


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


def test_cli_link_command_stamps_doc(runner: CliRunner, installed_repo: Path) -> None:
    new = runner.invoke(
        app, ["new-run", "--experiment", "E001", "--sp", "c=f"]
    )
    assert new.exit_code == 0
    short = next(t for t in new.stdout.split() if len(t) == 32)
    result = runner.invoke(
        app,
        ["link", short, "--experiment", "E099", "--hypothesis", "H099"],
    )
    assert result.exit_code == 0
    assert "linked" in result.stdout


# ---------------------------------------------------------------------------
# bind-tracker
# ---------------------------------------------------------------------------


def test_cli_bind_tracker_noop(runner: CliRunner, installed_repo: Path) -> None:
    new = runner.invoke(
        app, ["new-run", "--experiment", "E001", "--sp", "c=f"]
    )
    assert new.exit_code == 0
    short = next(t for t in new.stdout.split() if len(t) == 32)

    r = runner.invoke(app, ["bind-tracker", short, "--backend", "noop"])
    assert r.exit_code == 0, r.stdout
    assert "noop" in r.stdout


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_cli_validate_ok_on_fresh_install(runner: CliRunner, installed_repo: Path) -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "OK" in result.stdout


def test_cli_validate_fails_on_broken_run(runner: CliRunner, installed_repo: Path) -> None:
    # Create a run pointing at a non-existent experiment artifact.
    new = runner.invoke(
        app, ["new-run", "--experiment", "E999", "--sp", "c=f"]
    )
    assert new.exit_code == 0
    result = runner.invoke(app, ["validate", "--runs-only"])
    assert result.exit_code == 1
    assert "run.broken_experiment_link" in result.stdout


def test_cli_validate_kb_only_skips_runs(runner: CliRunner, installed_repo: Path) -> None:
    # Break only the runs side; kb-only should not notice.
    new = runner.invoke(
        app, ["new-run", "--experiment", "E999", "--sp", "c=f"]
    )
    assert new.exit_code == 0
    result = runner.invoke(app, ["validate", "--kb-only"])
    # Clean kb/ → 0.
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# sync-offline
# ---------------------------------------------------------------------------


def test_cli_sync_offline_reports_when_empty(
    runner: CliRunner, installed_repo: Path
) -> None:
    """With no offline-run directories on disk, the verb prints and exits 0."""
    result = runner.invoke(app, ["sync-offline"])
    assert result.exit_code == 0
    assert "no offline runs" in result.stdout


def test_cli_sync_offline_dry_run_lists_runs(
    runner: CliRunner, installed_repo: Path
) -> None:
    """With --dry-run, lists every offline run dir without invoking wandb."""
    fake = installed_repo / ".runs" / "workspace" / "jobA" / "wandb" / "offline-run-X"
    fake.mkdir(parents=True)
    result = runner.invoke(app, ["sync-offline", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "found 1 offline run" in result.stdout
    assert "dry-run" in result.stdout


# ---------------------------------------------------------------------------
# install-slash-commands
# ---------------------------------------------------------------------------


def test_cli_install_slash_commands(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["install-slash-commands"])
    assert result.exit_code == 0
    for name in (
        "aexp-new-hypothesis.md",
        "aexp-new-experiment.md",
        "aexp-new-run.md",
        "aexp-new-thread.md",
        "aexp-list-threads.md",
        "aexp-show-thread.md",
        "aexp-close-thread.md",
        "aexp-finding-from-run.md",
        "aexp-finding-from-batch.md",
        "aexp-finding-placeholder.md",
        "aexp-show-run.md",
        "aexp-show-batch.md",
        "aexp-list-runs.md",
        "aexp-status.md",
        "aexp-validate.md",
        "aexp-queue-add.md",
        "aexp-queue-list.md",
        "aexp-queue-materialize.md",
    ):
        dst = tmp_path / ".claude" / "commands" / name
        assert dst.is_file(), (name, result.stdout)
        body = dst.read_text(encoding="utf-8")
        # Must use the PATH-independent `python -m aexp` form (never the
        # Poetry shim `aexp` bin), so Claude Code's embedded bash can run it.
        assert "python -m aexp" in body, name
        # Must include the cross-platform fallback hint so agents know
        # how to recover when `python` doesn't resolve the package.
        assert "conda run" in body, name
        assert "conda_env_name" in body, name


def test_finding_slash_commands_route_through_new_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Finding-creation slash commands must call ``aexp new-finding``.

    ``aexp new-finding`` handles id allocation, template rendering, and
    (crucially) patches both parents' ``## Links`` sections automatically.
    If a slash command bypasses it — e.g. tells the agent to hand-write
    the markdown — ``kb_validate`` will reject the file on the next
    PostToolUse hook run because backlinks are missing from H### / E###.

    This test pins the invariant: every finding command must route
    through ``aexp new-finding`` and reference the ``## Links`` section
    (so the agent knows it's already handled).
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["install-slash-commands"])
    assert result.exit_code == 0
    for name in (
        "aexp-finding-from-run.md",
        "aexp-finding-from-batch.md",
        "aexp-finding-placeholder.md",
    ):
        body = (tmp_path / ".claude" / "commands" / name).read_text(encoding="utf-8")
        assert "aexp new-finding" in body, name
        assert "## Links" in body, name


# ---------------------------------------------------------------------------
# queue subcommands + run-queued
# ---------------------------------------------------------------------------


def _seed_hypothesis_and_experiment(
    repo: Path, *, runner_command: str | None = None
) -> None:
    """Create an H001 + E001 pair; optionally add a runner_command."""
    from aexp.artifacts import new_experiment, new_hypothesis

    new_hypothesis(title="h", repo_root=repo, artifact_id="H001")
    new_experiment(
        title="e", hypothesis_id="H001", repo_root=repo, artifact_id="E001"
    )
    if runner_command is not None:
        import frontmatter  # type: ignore[import-not-found]

        from aexp.kb_io import find_artifact_path

        exp_path = find_artifact_path("E001", kb_root=repo / "kb")
        post = frontmatter.load(str(exp_path))
        post["runner_command"] = runner_command
        exp_path.write_text(
            frontmatter.dumps(post) + "\n", encoding="utf-8"
        )


def test_cli_queue_add_single_then_list(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(installed_repo)
    r = runner.invoke(
        app,
        [
            "queue", "add",
            "--experiment", "E001",
            "--sp", "condition=full,seed=0",
            "--tag", "smoke",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "queued" in r.output

    lst = runner.invoke(app, ["queue", "list", "--tag", "smoke"])
    assert lst.exit_code == 0
    assert "full" in lst.output or "seed" in lst.output


def test_cli_queue_add_sweep_produces_cartesian_product(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(installed_repo)
    r = runner.invoke(
        app,
        [
            "queue", "add",
            "--experiment", "E001",
            "--sweep", "condition=full|classify, seed=0..3",
            "--tag", "sweep-test",
        ],
    )
    assert r.exit_code == 0, r.output
    # 2 × 4 = 8 jobs queued
    assert "8" in r.output

    lst = runner.invoke(
        app, ["queue", "list", "--tag", "sweep-test"]
    )
    assert lst.exit_code == 0
    # table title has the count
    assert "8" in lst.output


def test_cli_queue_add_sweep_and_sp_key_collision_errors(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(installed_repo)
    r = runner.invoke(
        app,
        [
            "queue", "add",
            "--experiment", "E001",
            "--sp", "seed=0",
            "--sweep", "seed=1..3",
        ],
    )
    assert r.exit_code != 0
    assert "seed" in r.output.lower()


def test_cli_queue_remove_abandons_job(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(installed_repo)
    r = runner.invoke(
        app,
        ["queue", "add", "--experiment", "E001", "--sp", "condition=full"],
    )
    assert r.exit_code == 0
    # Extract short job id — first 8 hex after "queued ".
    # Easier: grep the 32-hex substring.
    import re as _re
    m = _re.search(r"\b([0-9a-f]{32})\b", r.output)
    assert m, r.output
    full_id = m.group(1)

    rem = runner.invoke(app, ["queue", "remove", full_id])
    assert rem.exit_code == 0
    assert "abandoned" in rem.output.lower()


def test_cli_queue_materialize_shell_emits_script(
    runner: CliRunner, installed_repo: Path, tmp_path: Path
) -> None:
    _seed_hypothesis_and_experiment(installed_repo)
    for i in range(3):
        runner.invoke(
            app,
            [
                "queue", "add",
                "--experiment", "E001",
                "--sp", f"condition=full,seed={i}",
                "--tag", "mat",
            ],
        )
    out_path = installed_repo / "run_mat.sh"
    r = runner.invoke(
        app,
        [
            "queue", "materialize",
            "--runner", "shell",
            "--output", str(out_path),
            "--tag", "mat",
        ],
    )
    assert r.exit_code == 0, r.output
    assert out_path.is_file()
    body = out_path.read_text(encoding="utf-8")
    assert body.startswith("#!/usr/bin/env bash")
    assert body.count("aexp run-queued ") == 3


def test_cli_queue_materialize_slurm_with_kwargs(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(installed_repo)
    runner.invoke(
        app,
        [
            "queue", "add",
            "--experiment", "E001",
            "--sp", "condition=full",
            "--tag", "slurm-test",
        ],
    )
    out_path = installed_repo / "run.sbatch"
    r = runner.invoke(
        app,
        [
            "queue", "materialize",
            "--runner", "slurm",
            "--output", str(out_path),
            "--tag", "slurm-test",
            "--slurm-time", "04:00:00",
            "--slurm-mem", "32G",
        ],
    )
    assert r.exit_code == 0, r.output
    body = out_path.read_text(encoding="utf-8")
    assert "#SBATCH --array=0-0" in body
    assert "#SBATCH --time=04:00:00" in body
    assert "#SBATCH --mem=32G" in body


def test_cli_run_queued_executes_runner_command(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(
        installed_repo, runner_command="echo running-{condition}"
    )
    r_add = runner.invoke(
        app,
        ["queue", "add", "--experiment", "E001", "--sp", "condition=full"],
    )
    import re as _re
    m = _re.search(r"\b([0-9a-f]{32})\b", r_add.output)
    assert m, r_add.output
    job_id = m.group(1)

    r = runner.invoke(app, ["run-queued", job_id])
    assert r.exit_code == 0, r.output

    # Re-running should skip (idempotent).
    r2 = runner.invoke(app, ["run-queued", job_id])
    assert r2.exit_code == 0
    assert "skipping" in r2.output.lower()


def test_cli_queue_run_iterates_filter(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(
        installed_repo, runner_command="echo running-{condition}-{seed}"
    )
    for i in range(3):
        runner.invoke(
            app,
            [
                "queue", "add",
                "--experiment", "E001",
                "--sp", f"condition=full,seed={i}",
                "--tag", "qr",
            ],
        )
    r = runner.invoke(app, ["queue", "run", "--tag", "qr"])
    assert r.exit_code == 0, r.output
    assert "3/3" in r.output
    # Pending queue now empty (all jobs moved to complete).
    lst = runner.invoke(app, ["queue", "list", "--tag", "qr"])
    assert "0" in lst.output  # num entries reported in table title


def test_cli_queue_run_index_picks_single_job(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(
        installed_repo, runner_command="echo idx-{seed}"
    )
    for i in range(4):
        runner.invoke(
            app,
            [
                "queue", "add",
                "--experiment", "E001",
                "--sp", f"condition=full,seed={i}",
                "--tag", "idx",
            ],
        )
    r = runner.invoke(
        app, ["queue", "run", "--tag", "idx", "--index", "0"]
    )
    assert r.exit_code == 0, r.output
    assert "1/1" in r.output
    # Three jobs still pending.
    lst = runner.invoke(app, ["queue", "list", "--tag", "idx"])
    assert "3" in lst.output


def test_cli_queue_run_index_out_of_range_errors(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(
        installed_repo, runner_command="echo hi"
    )
    runner.invoke(
        app,
        ["queue", "add", "--experiment", "E001", "--sp", "condition=full",
         "--tag", "oob"],
    )
    r = runner.invoke(
        app, ["queue", "run", "--tag", "oob", "--index", "5"]
    )
    assert r.exit_code != 0
    assert "out of range" in r.output.lower()


def test_cli_run_queued_dry_run_prints_rendered_command(
    runner: CliRunner, installed_repo: Path
) -> None:
    _seed_hypothesis_and_experiment(
        installed_repo,
        runner_command="cmd-to-print condition={condition} seed={seed}",
    )
    r_add = runner.invoke(
        app,
        [
            "queue", "add",
            "--experiment", "E001",
            "--sp", "condition=full,seed=7",
        ],
    )
    import re as _re
    m = _re.search(r"\b([0-9a-f]{32})\b", r_add.output)
    job_id = m.group(1)
    r = runner.invoke(app, ["run-queued", job_id, "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "cmd-to-print condition=full seed=7" in r.output


# ---------------------------------------------------------------------------
# jupyter-setup
# ---------------------------------------------------------------------------


def test_jupyter_setup_dry_run_lists_commands_without_executing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`aexp jupyter-setup --dry-run` prints the four commands without running them."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)
    r = runner.invoke(app, ["jupyter-setup", "--dry-run"])
    assert r.exit_code == 0, r.output
    # Nothing actually executed.
    assert calls == []
    # All four operations are mentioned in the dry-run output.
    assert "disable jupyter_server_documents" in r.output
    assert "enable jupyter_server_ydoc" in r.output
    assert "enable jupyter_server_nbmodel" in r.output
    assert "disable @jupyter-ai-contrib/server-documents" in r.output


def test_jupyter_setup_executes_expected_commands(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --dry-run, runs the four expected jupyter commands via subprocess.run."""
    calls: list[list[str]] = []

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return _OK()

    monkeypatch.setattr("subprocess.run", fake_run)
    r = runner.invoke(app, ["jupyter-setup"])
    assert r.exit_code == 0, r.output
    assert len(calls) == 4
    joined = [" ".join(c) for c in calls]
    assert any("disable jupyter_server_documents" in c for c in joined)
    assert any("enable jupyter_server_ydoc" in c for c in joined)
    assert any("enable jupyter_server_nbmodel" in c for c in joined)
    assert any("disable @jupyter-ai-contrib/server-documents" in c for c in joined)


def test_jupyter_setup_propagates_failure(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a subprocess call fails, jupyter-setup exits nonzero and reports stderr."""

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "extension not installed"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Fail())
    r = runner.invoke(app, ["jupyter-setup"])
    assert r.exit_code != 0, r.output
    assert "FAILED" in r.output
    assert "extension not installed" in r.output
