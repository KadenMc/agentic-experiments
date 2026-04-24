"""Tests for the queue + materialization + sp-resolution layer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aexp.install import install_limina
from aexp.queue import (
    MaterializeResult,
    QueueEntry,
    RunnerCommandMissing,
    SubprocessFailed,
    SweepParseError,
    add_many_to_queue,
    add_to_queue,
    clear_queue,
    list_queue,
    materialize_queue,
    parse_sweep,
    remove_from_queue,
    render_runner_command,
    resolve_sp,
    run_queued,
)
from aexp.runs import create_run, open_run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_commit(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "-c", "user.email=t@e.com", "-c", "user.name=T",
            "commit", "-q", "-m", "init",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def installed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_commit(repo)
    install_limina(repo)
    return repo


def _make_experiment(
    repo: Path,
    *,
    experiment_id: str = "E001",
    hypothesis_id: str = "H001",
    conditions: dict | None = None,
    runner_command: str | None = None,
) -> None:
    """Create an H + E pair, then optionally patch the E frontmatter.

    Idempotent: if the H/E already exist on disk, skips creation and only
    patches the frontmatter. This lets tests call ``_make_experiment`` twice
    (once to set up, once to mutate conditions) without duplicate-id errors.
    """
    from aexp.artifacts import ArtifactCreateError, new_experiment, new_hypothesis

    try:
        new_hypothesis(title="hypothesis", repo_root=repo, artifact_id=hypothesis_id)
    except ArtifactCreateError:
        pass
    try:
        new_experiment(
            title="experiment",
            hypothesis_id=hypothesis_id,
            repo_root=repo,
            artifact_id=experiment_id,
        )
    except ArtifactCreateError:
        pass

    if conditions is None and runner_command is None:
        return

    # Patch the experiment's frontmatter.
    _patch_experiment_frontmatter(
        repo,
        experiment_id=experiment_id,
        conditions=conditions,
        runner_command=runner_command,
    )


def _patch_experiment_frontmatter(
    repo: Path,
    *,
    experiment_id: str = "E001",
    conditions: dict | None = None,
    runner_command: str | None = None,
) -> None:
    """Rewrite only the specified fields in an existing E###'s frontmatter."""
    from aexp.limina_io import find_artifact_path

    import frontmatter  # type: ignore[import-not-found]

    exp_path = find_artifact_path(experiment_id, kb_root=repo / "kb")
    post = frontmatter.load(str(exp_path))
    if runner_command is not None:
        post["runner_command"] = runner_command
    if conditions is not None:
        post["conditions"] = conditions
    exp_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1-3: add_to_queue basics
# ---------------------------------------------------------------------------


def test_add_to_queue_creates_signac_job_with_queued_status(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "full", "seed": 0},
        repo_root=installed_repo,
    )
    assert job.doc["status"] == "queued"
    assert job.sp["experiment_id"] == "E001"
    assert job.sp["condition"] == "full"
    assert job.sp["seed"] == 0


def test_add_to_queue_stamps_queue_doc_with_tag_and_timestamp(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "full"},
        tag="overnight",
        runner_hint="slurm",
        repo_root=installed_repo,
    )
    q = job.doc["queue"]
    assert q["tag"] == "overnight"
    assert q["runner_hint"] == "slurm"
    # ISO-8601-ish stamp (we don't parse; just assert non-empty).
    assert q["queued_at"] and isinstance(q["queued_at"], str)


def test_add_to_queue_inherits_create_run_behavior(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)
    job = add_to_queue(
        experiment_id="E001",
        hypothesis_id="H001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    # Auto-injected from create_run:
    assert "code_commit" in job.sp
    assert "code_dirty" in job.sp
    assert job.sp["hypothesis_id"] == "H001"
    # Limina link stamped:
    assert job.doc["limina"]["experiment_id"] == "E001"


# ---------------------------------------------------------------------------
# 4-7: list_queue filters
# ---------------------------------------------------------------------------


def test_list_queue_filters_by_experiment(installed_repo: Path) -> None:
    _make_experiment(installed_repo, experiment_id="E001")
    _make_experiment(
        installed_repo,
        experiment_id="E002",
        hypothesis_id="H002",
    )
    add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "a"},
        repo_root=installed_repo,
    )
    add_to_queue(
        experiment_id="E002",
        statepoint={"condition": "b"},
        repo_root=installed_repo,
    )
    e1 = list_queue(experiment_id="E001", repo_root=installed_repo)
    assert len(e1) == 1
    assert e1[0].experiment_id == "E001"


def test_list_queue_filters_by_tag(installed_repo: Path) -> None:
    _make_experiment(installed_repo)
    add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "a"},
        tag="overnight",
        repo_root=installed_repo,
    )
    add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "b"},
        tag="other",
        repo_root=installed_repo,
    )
    overnight = list_queue(tag="overnight", repo_root=installed_repo)
    assert len(overnight) == 1
    assert overnight[0].tag == "overnight"


def test_list_queue_excludes_terminal_statuses_by_default(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "a"},
        repo_root=installed_repo,
    )
    # Transition to complete.
    job.doc["status"] = "complete"
    assert list_queue(repo_root=installed_repo) == []


def test_list_queue_include_terminal_shows_complete_and_failed(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "a"},
        repo_root=installed_repo,
    )
    job.doc["status"] = "failed"
    with_term = list_queue(
        include_terminal=True, repo_root=installed_repo
    )
    assert len(with_term) == 1
    assert with_term[0].status == "failed"


# ---------------------------------------------------------------------------
# 8-9: remove / clear
# ---------------------------------------------------------------------------


def test_remove_from_queue_sets_abandoned(installed_repo: Path) -> None:
    _make_experiment(installed_repo)
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "a"},
        repo_root=installed_repo,
    )
    remove_from_queue(job.id, repo_root=installed_repo)
    reopened = open_run(job.id, repo_root=installed_repo)
    assert reopened.doc["status"] == "abandoned"


def test_clear_queue_bulk_abandons_by_tag(installed_repo: Path) -> None:
    _make_experiment(installed_repo)
    for i in range(3):
        add_to_queue(
            experiment_id="E001",
            statepoint={"condition": "full", "seed": i},
            tag="to-clear",
            repo_root=installed_repo,
        )
    abandoned = clear_queue(tag="to-clear", repo_root=installed_repo)
    assert len(abandoned) == 3
    assert list_queue(tag="to-clear", repo_root=installed_repo) == []


# ---------------------------------------------------------------------------
# 10-14: render_runner_command placeholders
# ---------------------------------------------------------------------------


def test_render_runner_command_substitutes_sp_keys() -> None:
    out = render_runner_command(
        "python train.py --condition {condition} --seed {seed}",
        {"condition": "full", "seed": 0},
        "abcdef1234567890abcdef1234567890",
    )
    assert out == "python train.py --condition full --seed 0"


def test_render_runner_command_leaves_shell_vars_alone() -> None:
    out = render_runner_command(
        "echo $HOSTNAME ${USER} {condition}",
        {"condition": "full"},
        "x" * 32,
    )
    # Shell vars untouched; only {condition} substituted.
    assert "$HOSTNAME" in out
    assert "${USER}" in out
    assert "full" in out


def test_render_runner_command_injects_job_id() -> None:
    out = render_runner_command(
        "echo {job_id}", {}, "abcdef1234567890abcdef1234567890"
    )
    assert out == "echo abcdef1234567890abcdef1234567890"


def test_render_runner_command_injects_sp_json() -> None:
    out = render_runner_command(
        "python train.py --config-json '{sp_json}'",
        {"condition": "full", "seed": 0, "max_turns": 12},
        "x" * 32,
    )
    assert "'{" in out
    # Pull the JSON blob between the single quotes.
    payload = out.split("'")[1]
    parsed = json.loads(payload)
    assert parsed == {"condition": "full", "seed": 0, "max_turns": 12}


def test_render_runner_command_ignores_unknown_placeholders() -> None:
    out = render_runner_command(
        "cmd {condition} {unknown_key} {also_unknown}",
        {"condition": "full"},
        "x" * 32,
    )
    assert "{unknown_key}" in out
    assert "{also_unknown}" in out
    assert "full" in out


# ---------------------------------------------------------------------------
# 15-18: resolve_sp
# ---------------------------------------------------------------------------


def test_resolve_sp_merges_named_condition_block(installed_repo: Path) -> None:
    _make_experiment(
        installed_repo,
        conditions={
            "full": {"model": "baseline", "max_turns": 12},
            "classify": {"model": "baseline", "max_turns": 4},
        },
    )
    resolved = resolve_sp(
        "E001", {"condition": "full", "seed": 0}, kb_root=installed_repo / "kb"
    )
    assert resolved["condition"] == "full"
    assert resolved["model"] == "baseline"
    assert resolved["max_turns"] == 12
    assert resolved["seed"] == 0


def test_resolve_sp_user_sp_wins_on_collision(installed_repo: Path) -> None:
    _make_experiment(
        installed_repo,
        conditions={"full": {"max_turns": 12}},
    )
    resolved = resolve_sp(
        "E001",
        {"condition": "full", "max_turns": 99},
        kb_root=installed_repo / "kb",
    )
    assert resolved["max_turns"] == 99  # user override wins


def test_resolve_sp_passes_through_when_condition_not_in_conditions_block(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo, conditions={"full": {"max_turns": 12}})
    resolved = resolve_sp(
        "E001",
        {"condition": "does_not_exist", "seed": 0},
        kb_root=installed_repo / "kb",
    )
    assert resolved == {"condition": "does_not_exist", "seed": 0}


def test_resolve_sp_passes_through_when_conditions_block_absent(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)  # no conditions: block
    resolved = resolve_sp(
        "E001",
        {"condition": "anything", "seed": 0},
        kb_root=installed_repo / "kb",
    )
    assert resolved == {"condition": "anything", "seed": 0}


# ---------------------------------------------------------------------------
# 19-20: sp resolution end-to-end + drift-proofing
# ---------------------------------------------------------------------------


def test_add_to_queue_resolves_sp_before_create_run(
    installed_repo: Path,
) -> None:
    _make_experiment(
        installed_repo,
        conditions={"full": {"model": "baseline", "max_turns": 12}},
    )
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "full", "seed": 0},
        repo_root=installed_repo,
    )
    # signac freezes sp to signac_statepoint.json — read it off disk
    # to prove resolution happened before job creation.
    sp_file = Path(job.path) / "signac_statepoint.json"
    frozen = json.loads(sp_file.read_text(encoding="utf-8"))
    assert frozen["model"] == "baseline"
    assert frozen["max_turns"] == 12
    assert frozen["condition"] == "full"


def test_add_to_queue_is_drift_proof(installed_repo: Path) -> None:
    _make_experiment(
        installed_repo,
        conditions={"full": {"model": "baseline", "max_turns": 12}},
    )
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    job_id = job.id

    # Mutate the conditions block after queueing.
    _make_experiment(
        installed_repo,
        conditions={"full": {"model": "baseline", "max_turns": 999}},
    )

    # Re-open; sp must still show the frozen-at-queue-time value.
    reopened = open_run(job_id, repo_root=installed_repo)
    assert reopened.sp["max_turns"] == 12


# ---------------------------------------------------------------------------
# 21-22: add_many_to_queue
# ---------------------------------------------------------------------------


def test_add_many_to_queue_cartesian_sweep(installed_repo: Path) -> None:
    _make_experiment(
        installed_repo,
        conditions={
            "full": {"max_turns": 12},
            "classify": {"max_turns": 4},
        },
    )
    jobs = add_many_to_queue(
        experiment_id="E001",
        sweep={"condition": ["full", "classify"], "seed": [0, 1, 2, 3]},
        repo_root=installed_repo,
    )
    assert len(jobs) == 8
    combos = {(job.sp["condition"], job.sp["seed"]) for job in jobs}
    assert combos == {
        (c, s) for c in ("full", "classify") for s in range(4)
    }


def test_add_many_to_queue_base_sp_applied_to_every_job(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)
    jobs = add_many_to_queue(
        experiment_id="E001",
        base_sp={"model": "fixed"},
        sweep={"seed": [0, 1]},
        repo_root=installed_repo,
    )
    assert all(job.sp["model"] == "fixed" for job in jobs)


def test_add_many_to_queue_rejects_overlap_between_base_and_sweep(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)
    with pytest.raises(ValueError):
        add_many_to_queue(
            experiment_id="E001",
            base_sp={"seed": 0},
            sweep={"seed": [1, 2]},
            repo_root=installed_repo,
        )


# ---------------------------------------------------------------------------
# Sweep grammar parser
# ---------------------------------------------------------------------------


def test_parse_sweep_enumerated_and_range() -> None:
    parsed = parse_sweep("condition=full|classify, seed=0..3")
    assert parsed == {
        "condition": ["full", "classify"],
        "seed": [0, 1, 2, 3],
    }


def test_parse_sweep_single_value_enum() -> None:
    parsed = parse_sweep("model=one")
    assert parsed == {"model": ["one"]}


def test_parse_sweep_reverse_range_errors() -> None:
    with pytest.raises(SweepParseError):
        parse_sweep("seed=3..0")


def test_parse_sweep_missing_equals_errors() -> None:
    with pytest.raises(SweepParseError):
        parse_sweep("no equals here")


# ---------------------------------------------------------------------------
# 27-31: materialize emitters
# ---------------------------------------------------------------------------


def test_materialize_shell_runner_emits_shebang_and_per_job_invocation(
    installed_repo: Path, tmp_path: Path
) -> None:
    _make_experiment(installed_repo)
    for i in range(3):
        add_to_queue(
            experiment_id="E001",
            statepoint={"condition": "full", "seed": i},
            repo_root=installed_repo,
        )
    out = tmp_path / "run.sh"
    result = materialize_queue(
        runner="shell", output_path=out, repo_root=installed_repo
    )
    assert result.num_jobs == 3
    assert result.runner == "shell"
    body = out.read_text(encoding="utf-8")
    assert body.startswith("#!/usr/bin/env bash")
    assert body.count("aexp run-queued ") == 3


def test_materialize_slurm_runner_emits_array_directive_and_job_bash_array(
    installed_repo: Path, tmp_path: Path
) -> None:
    _make_experiment(installed_repo)
    for i in range(4):
        add_to_queue(
            experiment_id="E001",
            statepoint={"condition": "full", "seed": i},
            tag="overnight",
            repo_root=installed_repo,
        )
    out = tmp_path / "run.sbatch"
    materialize_queue(
        runner="slurm",
        output_path=out,
        tag="overnight",
        slurm_kwargs={"time": "04:00:00", "mem": "32G"},
        repo_root=installed_repo,
    )
    body = out.read_text(encoding="utf-8")
    assert "#SBATCH --array=0-3" in body
    assert "#SBATCH --time=04:00:00" in body
    assert "#SBATCH --mem=32G" in body
    assert 'exec aexp run-queued "${jobs[$SLURM_ARRAY_TASK_ID]}"' in body
    # Bash array contains all 4 job ids.
    for line in body.splitlines():
        pass
    assert body.count("\n  ") >= 4  # 4 indented array entries


def test_materialize_manual_runner_emits_one_line_per_job_no_shebang(
    installed_repo: Path, tmp_path: Path
) -> None:
    _make_experiment(installed_repo)
    add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    out = tmp_path / "commands.txt"
    materialize_queue(
        runner="manual", output_path=out, repo_root=installed_repo
    )
    body = out.read_text(encoding="utf-8")
    assert not body.startswith("#!")
    assert body.count("aexp run-queued ") == 1


def test_materialize_respects_tag_filter(
    installed_repo: Path, tmp_path: Path
) -> None:
    _make_experiment(installed_repo)
    add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "a"},
        tag="wanted",
        repo_root=installed_repo,
    )
    add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "b"},
        tag="not-wanted",
        repo_root=installed_repo,
    )
    out = tmp_path / "run.sh"
    result = materialize_queue(
        runner="shell",
        output_path=out,
        tag="wanted",
        repo_root=installed_repo,
    )
    assert result.num_jobs == 1


def test_materialize_slurm_with_zero_jobs_errors(
    installed_repo: Path, tmp_path: Path
) -> None:
    _make_experiment(installed_repo)
    with pytest.raises(ValueError):
        materialize_queue(
            runner="slurm",
            output_path=tmp_path / "empty.sbatch",
            tag="no-such-tag",
            repo_root=installed_repo,
        )


# ---------------------------------------------------------------------------
# 32-41: run_queued lifecycle
# ---------------------------------------------------------------------------


def _queue_with_runner(
    installed_repo: Path,
    *,
    runner_command: str,
    statepoint: dict | None = None,
    conditions: dict | None = None,
) -> str:
    _make_experiment(
        installed_repo,
        runner_command=runner_command,
        conditions=conditions,
    )
    job = add_to_queue(
        experiment_id="E001",
        statepoint=statepoint or {"condition": "full"},
        repo_root=installed_repo,
    )
    return job.id


def test_run_queued_skips_complete_jobs_by_default(
    installed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    job_id = _queue_with_runner(
        installed_repo, runner_command="echo hi"
    )
    open_run(job_id, repo_root=installed_repo).doc["status"] = "complete"
    ret = run_queued(job_id, repo_root=installed_repo)
    assert ret == 0
    captured = capsys.readouterr()
    assert "skipping" in captured.out.lower()


def test_run_queued_skips_failed_unless_force(
    installed_repo: Path,
) -> None:
    job_id = _queue_with_runner(
        installed_repo, runner_command="echo hi"
    )
    open_run(job_id, repo_root=installed_repo).doc["status"] = "failed"
    # Without force: skip.
    assert run_queued(job_id, repo_root=installed_repo) == 0
    # Status stays failed.
    assert open_run(job_id, repo_root=installed_repo).doc["status"] == "failed"


def test_run_queued_marks_running_then_complete_on_success(
    installed_repo: Path,
) -> None:
    # `echo` returns 0 — job should transition to complete.
    job_id = _queue_with_runner(
        installed_repo,
        runner_command="echo hello from {condition}",
    )
    ret = run_queued(job_id, repo_root=installed_repo)
    assert ret == 0
    final = open_run(job_id, repo_root=installed_repo)
    assert final.doc["status"] == "complete"


def test_run_queued_marks_failed_and_captures_stderr_tail_on_nonzero_exit(
    installed_repo: Path,
) -> None:
    # Use python -c to emit to stderr and exit 1, portable across platforms.
    job_id = _queue_with_runner(
        installed_repo,
        runner_command=(
            f'"{sys.executable}" -c '
            '"import sys; sys.stderr.write(\'BOOM\'); sys.exit(1)"'
        ),
    )
    with pytest.raises(SubprocessFailed):
        run_queued(job_id, repo_root=installed_repo)
    final = open_run(job_id, repo_root=installed_repo)
    assert final.doc["status"] == "failed"
    err = final.doc["queue"]["last_error"]
    assert err["returncode"] == 1
    assert "BOOM" in err["stderr_tail"]


def test_run_queued_raises_runner_command_missing_when_experiment_lacks_template(
    installed_repo: Path,
) -> None:
    # Make experiment WITHOUT runner_command, then queue a job.
    _make_experiment(installed_repo)  # no runner_command
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "full"},
        repo_root=installed_repo,
    )
    with pytest.raises(RunnerCommandMissing):
        run_queued(job.id, repo_root=installed_repo)


def test_run_queued_honors_per_job_runner_command_override(
    installed_repo: Path,
) -> None:
    _make_experiment(installed_repo)  # no default runner_command
    job = add_to_queue(
        experiment_id="E001",
        statepoint={"condition": "full"},
        runner_command_override="echo override-path",
        repo_root=installed_repo,
    )
    ret = run_queued(job.id, repo_root=installed_repo)
    assert ret == 0
    assert open_run(job.id, repo_root=installed_repo).doc["status"] == "complete"


def test_run_queued_sets_aexp_job_id_env(
    installed_repo: Path, tmp_path: Path
) -> None:
    # Training script writes AEXP_JOB_ID to a file; we then assert it equals
    # the job id we queued.
    proof = tmp_path / "proof.txt"
    runner = (
        f'"{sys.executable}" -c '
        f'"import os; open(r\'{proof}\', \'w\').write(os.environ[\'AEXP_JOB_ID\'])"'
    )
    job_id = _queue_with_runner(installed_repo, runner_command=runner)
    run_queued(job_id, repo_root=installed_repo)
    assert proof.read_text(encoding="utf-8") == job_id


def test_run_queued_sets_aexp_job_workspace_env(
    installed_repo: Path, tmp_path: Path
) -> None:
    proof = tmp_path / "workspace.txt"
    runner = (
        f'"{sys.executable}" -c '
        f'"import os; open(r\'{proof}\', \'w\').write(os.environ[\'AEXP_JOB_WORKSPACE\'])"'
    )
    job_id = _queue_with_runner(installed_repo, runner_command=runner)
    run_queued(job_id, repo_root=installed_repo)
    workspace = Path(proof.read_text(encoding="utf-8"))
    assert workspace.is_dir()
    # Canonical: the workspace contains signac_statepoint.json.
    assert (workspace / "signac_statepoint.json").is_file()


def test_run_queued_dry_run_prints_command_without_executing(
    installed_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    job_id = _queue_with_runner(
        installed_repo,
        runner_command="echo should-not-actually-run {condition}",
    )
    ret = run_queued(job_id, dry_run=True, repo_root=installed_repo)
    assert ret == 0
    captured = capsys.readouterr()
    assert "should-not-actually-run full" in captured.out
    # Status stayed queued; never transitioned to running/complete.
    assert open_run(job_id, repo_root=installed_repo).doc["status"] == "queued"


def test_render_runner_command_sp_json_uses_compact_separators() -> None:
    """Unit-level check that `{sp_json}` produces whitespace-free JSON.

    End-to-end shell quoting of a JSON blob is platform-fragile (cmd.exe
    vs. POSIX sh handle quotes very differently), so we verify the
    renderer's output is whitespace-compact — that's what matters for
    downstream shell transport regardless of platform. A whitespace-ful
    JSON blob would split across argv on cmd.exe.
    """
    out = render_runner_command(
        "python train.py --cfg '{sp_json}'",
        {"condition": "full", "model": "baseline", "max_turns": 12, "seed": 0},
        "x" * 32,
    )
    # No whitespace inside the JSON payload.
    payload_start = out.index("{")
    payload_end = out.rindex("}") + 1
    payload = out[payload_start:payload_end]
    assert " " not in payload, payload
    parsed = json.loads(payload)
    assert parsed == {
        "condition": "full",
        "model": "baseline",
        "max_turns": 12,
        "seed": 0,
    }


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows cmd.exe eats JSON double quotes when a `{sp_json}` blob "
        "is splatted into an unquoted argv slot. The feature works on "
        "POSIX shells (bash/sh/zsh) — see the compact-separators unit test "
        "above for the platform-independent invariant. Real cluster use "
        "(Linux) is unaffected; Windows-local users should read "
        "signac_statepoint.json directly from $AEXP_JOB_WORKSPACE."
    ),
)
def test_run_queued_command_with_sp_json_renders_full_resolved_config(
    installed_repo: Path, tmp_path: Path
) -> None:
    """End-to-end: `{sp_json}` flows the resolved (merged) sp as JSON."""
    proof = tmp_path / "cfg.json"
    helper = tmp_path / "dump.py"
    helper.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path(r'{proof}').write_text(sys.argv[1], encoding='utf-8')\n",
        encoding="utf-8",
    )
    runner = f"'{sys.executable}' '{helper}' '{{sp_json}}'"
    job_id = _queue_with_runner(
        installed_repo,
        runner_command=runner,
        conditions={"full": {"model": "baseline", "max_turns": 12}},
    )
    run_queued(job_id, repo_root=installed_repo)
    cfg = json.loads(proof.read_text(encoding="utf-8"))
    assert cfg["model"] == "baseline"
    assert cfg["max_turns"] == 12
    assert cfg["condition"] == "full"


def test_run_queued_is_idempotent_across_script_reruns(
    installed_repo: Path,
) -> None:
    """Re-running `aexp run-queued <id>` on a complete job is a no-op."""
    job_id = _queue_with_runner(
        installed_repo, runner_command="echo hi"
    )
    assert run_queued(job_id, repo_root=installed_repo) == 0
    # Second invocation skips.
    assert run_queued(job_id, repo_root=installed_repo) == 0
    assert open_run(job_id, repo_root=installed_repo).doc["status"] == "complete"


# ---------------------------------------------------------------------------
# create_run integration — resolve_conditions kwarg
# ---------------------------------------------------------------------------


def test_create_run_resolves_conditions_by_default(installed_repo: Path) -> None:
    _make_experiment(
        installed_repo,
        conditions={"full": {"max_turns": 12}},
    )
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full", "seed": 0},
        repo_root=installed_repo,
    )
    assert job.sp["max_turns"] == 12


def test_kb_validate_passes_well_formed_conditions(installed_repo: Path) -> None:
    _make_experiment(
        installed_repo,
        conditions={
            "full": {"model": "baseline", "max_turns": 12, "tools": ["a", "b"]},
            "classify": {"model": "baseline", "max_turns": 4},
        },
    )
    from aexp.kb_validate import validate_kb

    result = validate_kb(installed_repo / "kb")
    schema_errors = [e for e in result.errors if e["check"] == "conditions_schema"]
    assert schema_errors == []


def test_kb_validate_rejects_conditions_not_a_dict(installed_repo: Path) -> None:
    _make_experiment(installed_repo)
    _patch_experiment_frontmatter(
        installed_repo,
        experiment_id="E001",
        conditions="not-a-dict",  # type: ignore[arg-type]
    )
    from aexp.kb_validate import validate_kb

    result = validate_kb(installed_repo / "kb")
    schema_errors = [e for e in result.errors if e["check"] == "conditions_schema"]
    assert len(schema_errors) >= 1
    assert "must be a mapping" in schema_errors[0]["message"]


def test_kb_validate_rejects_condition_block_not_a_dict(installed_repo: Path) -> None:
    _make_experiment(installed_repo)
    _patch_experiment_frontmatter(
        installed_repo,
        experiment_id="E001",
        conditions={"full": "bare-string"},  # should be a dict
    )
    from aexp.kb_validate import validate_kb

    result = validate_kb(installed_repo / "kb")
    schema_errors = [e for e in result.errors if e["check"] == "conditions_schema"]
    assert any("'full'" in e["message"] for e in schema_errors)


def test_create_run_skips_resolution_when_disabled(installed_repo: Path) -> None:
    _make_experiment(
        installed_repo,
        conditions={"full": {"max_turns": 12}},
    )
    job = create_run(
        experiment_id="E001",
        statepoint={"condition": "full", "seed": 0},
        resolve_conditions=False,
        repo_root=installed_repo,
    )
    assert "max_turns" not in job.sp
