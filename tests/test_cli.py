"""Integration tests for the Typer CLI — drives `aex` verbs against a real repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aexp.cli import app
from aexp.install import install_limina


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
    install_limina(repo)
    monkeypatch.chdir(repo)
    return repo


# ---------------------------------------------------------------------------
# version + install
# ---------------------------------------------------------------------------


def test_cli_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_cli_python_dash_m_invocation() -> None:
    """``python -m aexp version`` is the PATH-independent
    invocation path used by shipped slash commands. Must work on any shell
    where ``python`` has the package installed.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "aexp", "version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "0.1.0"


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
    for name in ("aexp-new-run.md", "aexp-close-run.md", "aexp-close-batch.md"):
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


def test_close_run_slash_command_warns_about_backlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner) -> None:
    """close-run / close-batch must tell the agent to add backlinks.

    kb_validate enforces bidirectional wiki-links; creating a finding
    without also editing H### and E### to link back will fail validation.
    Slash commands need to pre-warn so agents don't hit this every time.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["install-slash-commands"])
    assert result.exit_code == 0
    for name in ("aexp-close-run.md", "aexp-close-batch.md"):
        body = (tmp_path / ".claude" / "commands" / name).read_text(encoding="utf-8")
        assert "[[F" in body or "backlink" in body.lower(), name
        # Reminds about editing H### / E### Links sections
        assert "## Links" in body, name
