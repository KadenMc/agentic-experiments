"""Typer CLI — ``aex`` / ``agentic-experiments`` entry point.

Verbs are grouped by concern (install, runs, batches, link, tracker,
validate, slash-commands). Output uses ``rich`` for tables + colorized
summaries; every verb honors non-interactive use (tables render to plain
text when stdout is not a terminal).
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from aexp import __version__
from aexp.artifacts import (
    ArtifactCreateError,
    new_experiment,
    new_finding,
    new_hypothesis,
)
from aexp.install import install_limina
from aexp.linking import (
    link_to_experiment,
    list_batches,
    show_batch,
    summarize_run,
)
from aexp.queue import (
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
    run_queued,
)
from aexp.runs import create_run, find_runs, open_run
from aexp.trackers import NoopAdapter, TrackerInitError, bind_tracker
from aexp.validate import ValidateResult, validate_repo

app = typer.Typer(
    name="aex",
    help="Agentic Experiments — Limina + signac + W&B fusion layer.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sp_kv(spec: str | None) -> dict:
    """Parse ``"key=value,key2=value2"`` into a dict.

    Values are kept as strings; callers needing typed values should pass
    pre-typed sp via the Python API instead of the CLI.
    """
    if not spec:
        return {}
    out: dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise typer.BadParameter(f"sp entry missing '=': {chunk!r}")
        k, _, v = chunk.partition("=")
        out[k.strip()] = v.strip()
    return out


def _exit(code: int) -> None:  # pragma: no cover - trivial
    raise typer.Exit(code=code)


# ---------------------------------------------------------------------------
# version / install
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print package version."""
    typer.echo(__version__)


_INSTALL_HEADS_UP = """\
[bold]`aexp install` is about to modify the current repo:[/bold]

  - [cyan]kb/[/cyan]                     research-graph scaffold (hypotheses, experiments, findings)
  - [cyan]templates/[/cyan]              artifact templates (you can edit these)
  - [cyan].claude/settings.json[/cyan]   JSON-merge: our hooks added, your hooks + permissions preserved
  - [cyan].claude/skills/[/cyan]         4 research-methodology skills
  - [cyan].claude/commands/[/cyan]       9 slash commands (new/close H·E·F·run, list-runs, status, validate)
  - [cyan].mcp.json[/cyan]               JSON-merge: our `aexp` MCP server added, your other servers preserved
  - [cyan]AGENTS.md[/cyan], [cyan]CLAUDE.md[/cyan]       block-merge: your content outside our `<!-- agentic-experiments:begin/end -->` markers is preserved
  - [cyan].runs/[/cyan]                  signac project (idempotent; initialised if missing)
  - [cyan].aexp/installed.json[/cyan]   install marker with interpreter path + vendor sha

By default, conflicting existing files are [yellow]skipped with a warning[/yellow] — pass [bold]--force[/bold] to overwrite.
Hook scripts and validator code live inside the installed `aexp` package; no Python you didn't write lands in your repo.
"""


def _print_actions(actions: list, *, dry_run: bool) -> None:
    kinds: dict[str, int] = {}
    for a in actions:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
        if a.kind == "skipped_conflict":
            console.print(f"[yellow]{a.kind}[/yellow] {a.path}: {a.detail}")
        elif a.kind in ("merged_json", "merged_block", "wrote_marker", "initialized_runs"):
            console.print(f"[green]{a.kind}[/green] {a.path}")
    title = "dry-run plan" if dry_run else "install summary"
    table = Table(title=title, show_header=True)
    table.add_column("kind")
    table.add_column("count", justify="right")
    for k in sorted(kinds):
        table.add_row(k, str(kinds[k]))
    console.print(table)


_DEV_HEADS_UP = (
    "[bold magenta]Dev mode:[/bold magenta] `.mcp.json` will invoke the MCP server via your\n"
    "current Python interpreter (honours editable installs). The resulting\n"
    "[cyan].mcp.json[/cyan] bakes a machine-specific path — [yellow]do not commit it[/yellow]; gitignore\n"
    "while iterating, or re-run without `--dev` to regenerate the portable form.\n"
)


@app.command()
def install(
    run_store: str = typer.Option(".runs", "--run-store", help="Path for signac project."),
    force: bool = typer.Option(False, "--force", help="Overwrite conflicting user files."),
    assert_git: bool = typer.Option(
        True, "--require-git/--no-require-git", help="Require a .git dir at repo root."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Preview what would change without writing anything.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the pre-install confirmation prompt.",
    ),
    dev: bool = typer.Option(
        False,
        "--dev",
        "-D",
        help=(
            "Write `.mcp.json` using the current Python interpreter "
            "(`\"<python_exe>\" -m aexp.mcp_server`) instead of the portable "
            "uvx/PyPI form. Use when you're developing aexp locally and want "
            "editable-install edits to reach the MCP surface. The resulting "
            "`.mcp.json` is machine-specific — do not commit."
        ),
    ),
) -> None:
    """Install the aexp harness into the current repo.

    By default, shows a summary of what will be modified and asks for
    confirmation before making any changes. Use ``--yes`` to skip the
    prompt (for scripted / CI use) or ``--dry-run`` to preview only.
    Pass ``--dev`` to write a development-mode ``.mcp.json`` that honours
    editable installs.
    """
    import sys as _sys

    cwd = Path.cwd()

    # The dev-mode advisory is important enough that we always emit it when
    # --dev is set — even under --yes, where the full heads-up is skipped.
    # Users should not accidentally commit a machine-specific .mcp.json.
    if dev:
        console.print(_DEV_HEADS_UP)

    if dry_run:
        console.print(_INSTALL_HEADS_UP)
        actions = install_limina(
            cwd,
            run_store=run_store,
            force=force,
            assert_git=assert_git,
            dry_run=True,
            dev=dev,
        )
        _print_actions(actions, dry_run=True)
        return

    if not yes:
        console.print(_INSTALL_HEADS_UP)
        if not _sys.stdin.isatty():
            console.print(
                "[yellow]No TTY attached; rerun with --yes to confirm or --dry-run to preview.[/yellow]"
            )
            raise typer.Exit(code=1)
        if not typer.confirm("Proceed with install?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=1)

    actions = install_limina(
        cwd, run_store=run_store, force=force, assert_git=assert_git, dev=dev
    )
    _print_actions(actions, dry_run=False)


# ---------------------------------------------------------------------------
# artifact creation (H / E / F)
# ---------------------------------------------------------------------------


def _print_artifact_result(label: str, result) -> None:  # type: ignore[no-untyped-def]
    console.print(f"[green]created[/green] {label} [bold]{result.artifact_id}[/bold]")
    console.print(f"  path: {result.path}")
    if result.backlinks_patched:
        console.print(
            f"  backlinks patched: {', '.join(result.backlinks_patched)}"
        )
    if result.backlinks_already_present:
        console.print(
            f"  backlinks already present: {', '.join(result.backlinks_already_present)}"
        )


@app.command("new-hypothesis")
def new_hypothesis_cmd(
    title: str = typer.Option(..., "--title", help="Human-readable hypothesis title."),
    id: str | None = typer.Option(None, "--id", help="Force a specific H### id."),
    link: list[str] = typer.Option(
        [], "--link", help="Extra target to add to ## Links (repeatable)."
    ),
) -> None:
    """Create a new hypothesis (H###) with a validator-clean skeleton."""
    try:
        result = new_hypothesis(title=title, artifact_id=id, extra_links=link or None)
    except ArtifactCreateError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
    _print_artifact_result("hypothesis", result)


@app.command("new-experiment")
def new_experiment_cmd(
    title: str = typer.Option(..., "--title", help="Human-readable experiment title."),
    hypothesis: str = typer.Option(
        ..., "--hypothesis", help="Parent H### id (must exist on disk)."
    ),
    id: str | None = typer.Option(None, "--id", help="Force a specific E### id."),
    link: list[str] = typer.Option(
        [], "--link", help="Extra target to add to ## Links (repeatable)."
    ),
) -> None:
    """Create a new experiment (E###) under an existing hypothesis."""
    try:
        result = new_experiment(
            title=title,
            hypothesis_id=hypothesis,
            artifact_id=id,
            extra_links=link or None,
        )
    except ArtifactCreateError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
    _print_artifact_result("experiment", result)


@app.command("new-finding")
def new_finding_cmd(
    title: str = typer.Option(..., "--title", help="Human-readable finding title."),
    hypothesis: str = typer.Option(..., "--hypothesis", help="Parent H### id."),
    experiment: str = typer.Option(..., "--experiment", help="Parent E### id."),
    impact: str = typer.Option(
        "MEDIUM", "--impact", help="CRITICAL | HIGH | MEDIUM | LOW."
    ),
    id: str | None = typer.Option(None, "--id", help="Force a specific F### id."),
    link: list[str] = typer.Option(
        [], "--link", help="Extra target to add to ## Links (repeatable)."
    ),
) -> None:
    """Create a new finding (F###) citing one hypothesis + one experiment.

    This writes the finding skeleton and patches both parents' ``## Links``
    sections. The ``supporting_runs:`` citation is added separately — use
    ``/aexp-finding-from-run`` (single job), ``/aexp-finding-from-batch``
    (batch selector), or ``/aexp-finding-placeholder`` (no citations yet)
    depending on what the finding cites.
    """
    try:
        result = new_finding(
            title=title,
            hypothesis_id=hypothesis,
            experiment_id=experiment,
            impact=impact,
            artifact_id=id,
            extra_links=link or None,
        )
    except ArtifactCreateError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
    _print_artifact_result("finding", result)


# ---------------------------------------------------------------------------
# run creation + browsing
# ---------------------------------------------------------------------------


@app.command("new-run")
def new_run(
    experiment: str = typer.Option(..., "--experiment", help="Limina E### id."),
    hypothesis: str | None = typer.Option(None, "--hypothesis"),
    sub_hypothesis: str | None = typer.Option(None, "--sub-hypothesis"),
    sp: str | None = typer.Option(None, "--sp", help="KEY=VAL,KEY=VAL state-point params."),
    no_commit: bool = typer.Option(False, "--no-commit", help="Skip code_commit/code_dirty in sp."),
) -> None:
    """Create a signac job linked to a Limina experiment."""
    statepoint = _parse_sp_kv(sp)
    job = create_run(
        experiment_id=experiment,
        hypothesis_id=hypothesis,
        sub_hypothesis_id=sub_hypothesis,
        statepoint=statepoint,
        include_commit=not no_commit,
    )
    console.print(f"[green]created[/green] job [bold]{job.id}[/bold]")
    console.print(f"  workspace: {job.path}")


@app.command("list-runs")
def list_runs_cmd(
    experiment: str | None = typer.Option(None, "--experiment"),
    hypothesis: str | None = typer.Option(None, "--hypothesis"),
    status: str | None = typer.Option(None, "--status"),
    sp: str | None = typer.Option(None, "--sp", help="Exact-match filter KEY=VAL,..."),
) -> None:
    """List signac jobs filtered by Limina link + sp."""
    sp_filters = _parse_sp_kv(sp)
    jobs = find_runs(
        experiment_id=experiment,
        hypothesis_id=hypothesis,
        status=status,  # type: ignore[arg-type]
        **sp_filters,
    )
    table = Table(title=f"runs ({len(jobs)})", show_header=True)
    for col in ("short_id", "experiment", "hypothesis", "status", "condition", "tracker"):
        table.add_column(col)
    for job in jobs:
        s = summarize_run(job)
        cond = job.sp.get("condition")
        tracker_url = s.tracker_url or ""
        table.add_row(
            s.job_id[:8],
            s.experiment_id or "",
            s.hypothesis_id or "",
            s.status or "",
            str(cond) if cond is not None else "",
            tracker_url,
        )
    console.print(table)


@app.command("list-batches")
def list_batches_cmd(
    experiment: str | None = typer.Option(None, "--experiment"),
) -> None:
    """List distinct (experiment, condition) slices over runs."""
    batches = list_batches(experiment_id=experiment)
    table = Table(title=f"batches ({len(batches)})", show_header=True)
    for col in ("experiment", "slug", "selector", "count", "statuses", "tracker_group"):
        table.add_column(col)
    for b in batches:
        sel_s = ", ".join(f"{k}={v!r}" for k, v in b.selector.items()) or "(none)"
        status_s = ", ".join(f"{k}:{v}" for k, v in sorted(b.status_counts.items()))
        table.add_row(
            b.experiment_id,
            b.batch_slug,
            sel_s,
            str(b.count),
            status_s,
            b.tracker_group or "",
        )
    console.print(table)


@app.command("show-run")
def show_run(job_id: str) -> None:
    """Show state point, doc, linked Limina frame for a run."""
    job = open_run(job_id)
    s = summarize_run(job)
    console.print(f"[bold]{job.id}[/bold] ({s.batch_slug})")
    console.print(f"  experiment: [cyan]{s.experiment_id}[/cyan]")
    console.print(f"  hypothesis: [cyan]{s.hypothesis_id}[/cyan]")
    console.print(f"  status: {s.status}")
    console.print(f"  started_at: {s.started_at}")
    console.print(f"  ended_at: {s.ended_at}")
    console.print(f"  tracker_url: {s.tracker_url or '(none)'}")
    console.print("  state_point:")
    for k, v in sorted(s.sp.items()):
        console.print(f"    {k} = {v!r}")


@app.command("show-batch")
def show_batch_cmd(
    experiment: str = typer.Option(..., "--experiment"),
    condition: str | None = typer.Option(None, "--condition"),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    """Show an aggregate view of a batch slice."""
    selector: dict = {}
    if condition is not None:
        selector["condition"] = condition
    if model is not None:
        selector["model"] = model
    rows = show_batch(experiment_id=experiment, selector=selector)
    table = Table(
        title=f"batch {experiment} {selector or '(no filter)'}: {len(rows)} run(s)",
        show_header=True,
    )
    for col in ("short_id", "status", "condition", "sp", "tracker"):
        table.add_column(col)
    for s in rows:
        table.add_row(
            s.job_id[:8],
            s.status or "",
            str(s.sp.get("condition", "")),
            ", ".join(f"{k}={v!r}" for k, v in sorted(s.sp.items()) if k != "condition"),
            s.tracker_url or "",
        )
    console.print(table)


@app.command()
def link(
    job_id: str,
    experiment: str = typer.Option(..., "--experiment"),
    hypothesis: str | None = typer.Option(None, "--hypothesis"),
    sub_hypothesis: str | None = typer.Option(None, "--sub-hypothesis"),
) -> None:
    """Retroactively stamp ``job.doc['limina']`` onto an existing run."""
    link_to_experiment(
        job_id,
        experiment_id=experiment,
        hypothesis_id=hypothesis,
        sub_hypothesis_id=sub_hypothesis,
    )
    console.print(f"[green]linked[/green] {job_id[:8]} -> {experiment}")


# ---------------------------------------------------------------------------
# trackers
# ---------------------------------------------------------------------------


@app.command("bind-tracker")
def bind_tracker_cmd(
    job_id: str,
    backend: str = typer.Option("noop", "--backend", help="noop | wandb"),
    project: str | None = typer.Option(None, "--project"),
    offline: bool = typer.Option(False, "--offline"),
) -> None:
    """Attach a tracker run (e.g. W&B) to an existing signac job."""
    if backend == "noop":
        adapter = NoopAdapter()
    elif backend == "wandb":
        from aexp.trackers import WandbAdapter  # lazy

        try:
            adapter = WandbAdapter()
        except TrackerInitError as exc:
            console.print(f"[red]{exc}[/red]")
            _exit(2)
    else:
        console.print(f"[red]unknown backend[/red]: {backend!r}")
        _exit(2)
    if backend == "wandb" and not project:
        console.print("[red]--project is required for --backend wandb[/red]")
        _exit(2)

    job = open_run(job_id)
    handle = bind_tracker(
        job,
        adapter,
        project=project or "agentic-experiments-default",
        offline=offline,
    )
    console.print(f"[green]bound[/green] {backend}: run_id={handle.id} group={handle.group}")
    if handle.url:
        console.print(f"  url: {handle.url}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@app.command()
def validate(
    kb_only: bool = typer.Option(False, "--kb-only"),
    runs_only: bool = typer.Option(False, "--runs-only"),
) -> None:
    """Validate Limina KB + run-link integrity."""
    if kb_only and runs_only:
        console.print("[red]cannot combine --kb-only and --runs-only[/red]")
        _exit(2)
    mode = "kb-only" if kb_only else ("runs-only" if runs_only else "full")
    result: ValidateResult = validate_repo(mode=mode)  # type: ignore[arg-type]
    for issue in result.issues:
        color = "red" if issue.severity == "error" else "yellow"
        tag = issue.severity.upper()
        loc = f" [{issue.path}]" if issue.path else ""
        console.print(f"[{color}]{tag}[/{color}] {issue.code}{loc}: {issue.message}")
    if result.ok:
        console.print(f"[green]OK[/green] no validation errors ({len(result.warnings)} warnings)")
        return
    console.print(
        f"[red]FAILED[/red] {len(result.errors)} error(s), {len(result.warnings)} warning(s)"
    )
    _exit(1)


# ---------------------------------------------------------------------------
# slash-command templates
# ---------------------------------------------------------------------------


@app.command("sync-offline")
def sync_offline_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List offline runs that would be synced, without calling wandb."
    ),
) -> None:
    """Walk the run store and ``wandb sync`` every offline run found.

    For HPC compute nodes without internet: runs are written as
    ``<workspace>/wandb/offline-run-*/`` during execution, then synced from a
    login node later with this verb (or ``wandb sync --sync-all`` directly).
    """
    from aexp.trackers import find_offline_runs, sync_offline_runs
    from aexp.utils.paths import find_repo_root, resolve_run_store_path

    repo_root = find_repo_root()
    run_store = resolve_run_store_path(repo_root)
    if not run_store.is_dir():
        console.print(f"[red]run store not found[/red] at {run_store}")
        _exit(2)

    runs = find_offline_runs(run_store)
    if not runs:
        console.print(f"[yellow]no offline runs found under[/yellow] {run_store}")
        return

    console.print(f"found {len(runs)} offline run(s) under {run_store}:")
    for r in runs:
        console.print(f"  {r}")

    results = sync_offline_runs(run_store, dry_run=dry_run)
    failures = 0
    for res in results:
        tag = "dry-run" if dry_run else ("ok" if res.ok else "FAIL")
        color = "cyan" if dry_run else ("green" if res.ok else "red")
        console.print(f"[{color}]{tag}[/{color}] {res.path}")
        if not res.ok and not dry_run:
            failures += 1
            if res.stderr:
                console.print(f"  stderr: {res.stderr.strip()}")
    if failures:
        console.print(f"[red]{failures} sync(s) failed[/red]")
        _exit(1)


@app.command("install-slash-commands")
def install_slash_commands(
    target: str = typer.Option(".claude/commands", "--target"),
    force: bool = typer.Option(
        True,
        "--force/--no-force",
        help=(
            "Overwrite existing slash-command files with the shipped versions. "
            "Default True (preserves the pre-0.1.2 behaviour of this verb); "
            "pass ``--no-force`` to skip files you've customized."
        ),
    ),
) -> None:
    """Copy shipped slash commands into ``<target>/``.

    ``aexp install`` now runs this as part of the standard setup, so this verb
    is mainly for re-installs or for copying to a custom target directory.
    """
    from aexp.install import _install_slash_commands

    actions = _install_slash_commands(Path.cwd(), target_rel=target, force=force)
    for a in actions:
        if a.kind == "skipped_conflict":
            console.print(f"[yellow]{a.kind}[/yellow] {a.path}: {a.detail}")
        elif a.kind == "skipped_identical":
            console.print(f"[dim]skipped_identical[/dim] {a.path}")
        elif a.kind == "copied":
            console.print(f"[green]copied[/green] {a.path}")
    if not actions:
        console.print("[yellow]no slash commands to install[/yellow]")


# ---------------------------------------------------------------------------
# queue subcommand group — pending-run registration + runner materialization
# ---------------------------------------------------------------------------


queue_app = typer.Typer(
    help="Register pending runs; materialize them as a runner script.",
    no_args_is_help=True,
)
app.add_typer(queue_app, name="queue")


def _parse_sweep_or_exit(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return parse_sweep(raw)
    except SweepParseError as exc:
        console.print(f"[red]invalid --sweep spec: {exc}[/red]")
        _exit(2)
    return {}  # unreachable; _exit raises


def _parse_slurm_kwargs(
    time: str | None,
    mem: str | None,
    gpus: str | None,
    partition: str | None,
    account: str | None,
    extra: str | None,
) -> dict:
    kw: dict[str, str] = {}
    if time is not None:
        kw["time"] = time
    if mem is not None:
        kw["mem"] = mem
    if gpus is not None:
        kw["gpus"] = gpus
    if partition is not None:
        kw["partition"] = partition
    if account is not None:
        kw["account"] = account
    if extra is not None:
        kw["extra"] = extra
    return kw


@queue_app.command("add")
def queue_add_cmd(
    experiment: str = typer.Option(..., "--experiment", help="Limina E### id."),
    hypothesis: str | None = typer.Option(None, "--hypothesis"),
    sp: str | None = typer.Option(
        None, "--sp", help="Fixed sp values: KEY=VAL,KEY=VAL."
    ),
    sweep: str | None = typer.Option(
        None,
        "--sweep",
        help=(
            'Cartesian sweep: "KEY=V1|V2|V3, KEY2=0..3". Pipe-separated '
            "enum values; integer range via a..b inclusive. Combines with "
            "--sp for fixed values."
        ),
    ),
    tag: str | None = typer.Option(None, "--tag", help="Groups queued jobs."),
    runner_hint: str | None = typer.Option(
        None, "--runner-hint", help="Suggest runner for materialize."
    ),
    no_commit: bool = typer.Option(
        False, "--no-commit", help="Skip code_commit/code_dirty in sp."
    ),
    no_resolve: bool = typer.Option(
        False,
        "--no-resolve",
        help=(
            "Skip condition-block resolution. By default, if sp.condition "
            "names a key in the experiment's conditions: frontmatter, the "
            "block is merged into sp."
        ),
    ),
) -> None:
    """Register one or more pending runs."""
    base_sp = _parse_sp_kv(sp)
    sweep_dict = _parse_sweep_or_exit(sweep)

    overlap = set(base_sp) & set(sweep_dict)
    if overlap:
        console.print(
            f"[red]--sp and --sweep share keys: {sorted(overlap)}; "
            "put each key in exactly one.[/red]"
        )
        _exit(2)

    if sweep_dict:
        jobs = add_many_to_queue(
            experiment_id=experiment,
            hypothesis_id=hypothesis,
            base_sp=base_sp,
            sweep=sweep_dict,
            tag=tag,
            runner_hint=runner_hint,
            include_commit=not no_commit,
            resolve_conditions=not no_resolve,
        )
        console.print(
            f"[green]queued[/green] [bold]{len(jobs)}[/bold] job(s)"
            + (f" under tag=[cyan]{tag}[/cyan]" if tag else "")
        )
        for job in jobs:
            console.print(f"  {job.id[:8]}  {dict(job.sp)}")
        return

    job = add_to_queue(
        experiment_id=experiment,
        hypothesis_id=hypothesis,
        statepoint=base_sp,
        tag=tag,
        runner_hint=runner_hint,
        include_commit=not no_commit,
        resolve_conditions=not no_resolve,
    )
    console.print(f"[green]queued[/green] [bold]{job.id}[/bold]")
    console.print(f"  workspace: {job.path}")
    if tag:
        console.print(f"  tag: {tag}")


@queue_app.command("list")
def queue_list_cmd(
    experiment: str | None = typer.Option(None, "--experiment"),
    tag: str | None = typer.Option(None, "--tag"),
    include_terminal: bool = typer.Option(
        False,
        "--include-terminal",
        help="Show jobs in terminal states (complete/failed/abandoned).",
    ),
) -> None:
    """List queued runs (pending-run rows only by default)."""
    entries = list_queue(
        experiment_id=experiment,
        tag=tag,
        include_terminal=include_terminal,
    )
    table = Table(
        title=f"queue ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'})",
        show_header=True,
    )
    for col in ("short_id", "experiment", "status", "tag", "sp"):
        table.add_column(col)
    for e in entries:
        sp_s = ", ".join(
            f"{k}={v!r}"
            for k, v in sorted(e.sp.items())
            if k not in ("experiment_id", "code_commit", "code_dirty")
        )
        table.add_row(
            e.job_id[:8],
            e.experiment_id or "",
            e.status or "",
            e.tag or "",
            sp_s,
        )
    console.print(table)


@queue_app.command("remove")
def queue_remove_cmd(job_id: str) -> None:
    """Mark one queued job ``abandoned`` without executing it."""
    remove_from_queue(job_id)
    console.print(f"[yellow]abandoned[/yellow] {job_id[:8]}")


@queue_app.command("clear")
def queue_clear_cmd(
    experiment: str | None = typer.Option(None, "--experiment"),
    tag: str | None = typer.Option(None, "--tag"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Bulk-abandon queued jobs matching the filter."""
    import sys as _sys

    entries = list_queue(experiment_id=experiment, tag=tag)
    if not entries:
        console.print("[yellow]no queued jobs match the filter.[/yellow]")
        return
    if not yes:
        console.print(
            f"[yellow]about to abandon {len(entries)} queued job(s).[/yellow]"
        )
        if not _sys.stdin.isatty():
            console.print(
                "[yellow]No TTY; rerun with --yes to confirm.[/yellow]"
            )
            _exit(1)
        if not typer.confirm("Proceed?", default=False):
            console.print("[yellow]aborted.[/yellow]")
            return
    abandoned = clear_queue(experiment_id=experiment, tag=tag)
    console.print(f"[yellow]abandoned[/yellow] {len(abandoned)} job(s)")


@queue_app.command("materialize")
def queue_materialize_cmd(
    runner: str = typer.Option(
        "shell", "--runner", help="shell | slurm | manual"
    ),
    output: str = typer.Option(
        "run_queue.sh", "--output", "-o", help="Output path."
    ),
    experiment: str | None = typer.Option(None, "--experiment"),
    tag: str | None = typer.Option(None, "--tag"),
    slurm_time: str | None = typer.Option(
        None, "--slurm-time", help="#SBATCH --time value (e.g. 04:00:00)."
    ),
    slurm_mem: str | None = typer.Option(
        None, "--slurm-mem", help="#SBATCH --mem value (e.g. 32G)."
    ),
    slurm_gpus: str | None = typer.Option(
        None, "--slurm-gpus", help="#SBATCH --gpus value."
    ),
    slurm_partition: str | None = typer.Option(
        None, "--slurm-partition"
    ),
    slurm_account: str | None = typer.Option(None, "--slurm-account"),
    slurm_extra: str | None = typer.Option(
        None,
        "--slurm-extra",
        help="Free-form #SBATCH lines (newline-separated). Appended verbatim.",
    ),
) -> None:
    """Emit a runner script covering every matching queue entry."""
    if runner not in ("shell", "slurm", "manual"):
        console.print(
            f"[red]unknown runner {runner!r}; expected shell|slurm|manual[/red]"
        )
        _exit(2)

    slurm_kwargs = _parse_slurm_kwargs(
        slurm_time, slurm_mem, slurm_gpus, slurm_partition,
        slurm_account, slurm_extra,
    )
    try:
        result = materialize_queue(
            runner=runner,  # type: ignore[arg-type]
            output_path=output,
            experiment_id=experiment,
            tag=tag,
            slurm_kwargs=slurm_kwargs or None,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
        return
    console.print(
        f"[green]materialized[/green] [bold]{result.num_jobs}[/bold] "
        f"job(s) → {result.output_path}"
    )
    if runner == "shell":
        console.print(f"  run it: [cyan]bash {output}[/cyan]")
    elif runner == "slurm":
        console.print(f"  submit it: [cyan]sbatch {output}[/cyan]")
    elif runner == "manual":
        console.print(
            "  copy the commands from the output file into your runner."
        )


# ---------------------------------------------------------------------------
# run-queued — runner-side execution of one queued job
# ---------------------------------------------------------------------------


@app.command("run-queued")
def run_queued_cmd(
    job_id: str,
    force: bool = typer.Option(
        False, "--force", help="Re-run even if status is terminal."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the rendered command without executing."
    ),
) -> None:
    """Execute one queued job. Idempotent: terminal states skip unless --force.

    Invoked per-job by materialized runner scripts. Reads the experiment's
    ``runner_command`` template (or per-job override), renders it against
    the job's resolved sp, and runs it via ``subprocess.run(shell=True)``
    inside aexp's ``run_lifecycle`` so status transitions happen
    automatically.
    """
    try:
        returncode = run_queued(job_id, force=force, dry_run=dry_run)
    except RunnerCommandMissing as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
        return
    except SubprocessFailed as exc:
        console.print(f"[red]runner failed: {exc}[/red]")
        _exit(1)
        return
    if returncode != 0:
        _exit(returncode)


if __name__ == "__main__":
    app()
