"""Typer CLI — ``aex`` / ``agentic-experiments`` entry point.

Verbs are grouped by concern (install, runs, batches, link, tracker,
validate, slash-commands). Output uses ``rich`` for tables + colorized
summaries; every verb honors non-interactive use (tables render to plain
text when stdout is not a terminal).
"""
from __future__ import annotations

import warnings
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from aexp import __version__
from aexp.airgapped._relay import airgapped_app
from aexp.artifacts import (
    ArtifactCreateError,
    close_thread,
    new_experiment,
    new_finding,
    new_hypothesis,
    new_thread,
)
from aexp.install import InstallRefused, install_scaffold
from aexp.kb_io import (
    ArtifactNotFoundError,
    list_kb_artifacts,
    load_thread,
)
from aexp.linking import (
    link_to_experiment,
    list_batches,
    show_batch,
    summarize_run,
)
from aexp.queue import (
    DuplicatePendingJobWarning,
    RunnerCommandMissing,
    StopJobError,
    SubprocessFailed,
    SweepParseError,
    add_many_to_queue,
    add_to_queue,
    clear_queue,
    list_queue,
    materialize_queue,
    parse_sweep,
    remove_from_queue,
    run_queue,
    run_queued,
    stop_queued,
)
from aexp.runs import create_run, find_runs, open_run
from aexp.sandbox import (
    SandboxScaffoldError,
)
from aexp.sandbox import (
    scaffold as scaffold_sandbox,
)
from aexp.trackers import NoopAdapter, TrackerInitError, bind_tracker
from aexp.validate import ValidateResult, validate_repo

app = typer.Typer(
    name="aex",
    help="Agentic Experiments — signac + W&B experiment-tracking fusion layer.",
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


@contextmanager
def _capture_dup_warnings():
    """Catch DuplicatePendingJobWarning during a queue-add CLI invocation.

    The Python API surfaces dedupe events via ``warnings.warn`` so library
    consumers can route them however they like (logger, error, ignore).
    The CLI converts them into a single rich-styled summary line so the
    user gets actionable feedback without a stderr dump of the warning's
    internals.

    Usage::

        with _capture_dup_warnings() as captured:
            jobs = add_many_to_queue(...)
        if captured:
            console.print("[yellow]skipped N duplicate(s)[/yellow]")

    The ``captured`` list is populated by the context manager's ``__exit__``
    so it is *empty* during the with-block body and *full* afterward.
    """
    captured: list[warnings.WarningMessage] = []
    with warnings.catch_warnings(record=True) as raw:
        warnings.simplefilter("always", DuplicatePendingJobWarning)
        yield captured
        for entry in raw:
            if issubclass(entry.category, DuplicatePendingJobWarning):
                captured.append(entry)


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
  - [cyan].claude/commands/[/cyan]       21 slash commands (H/E/F/T artifacts, runs, queue, status, validate, jupyter-iterate, promote-nb)
  - [cyan].mcp.json[/cyan]               JSON-merge: our `aexp` MCP server added, your other servers preserved
  - [cyan]AGENTS.md[/cyan], [cyan]CLAUDE.md[/cyan]       block-merge: your content outside our `<!-- agentic-experiments:begin/end -->` markers is preserved
  - [cyan].runs/[/cyan]                  signac project (idempotent; initialised if missing)
  - [cyan].aexp/installed.json[/cyan]   install marker with interpreter path + scaffold sha

By default, conflicting existing files are [yellow]skipped with a warning[/yellow] — pass [bold]--force[/bold] to overwrite.
[bold]User-authored scaffold content under `kb/` and `templates/` is preserved even under --force[/bold] (see `preserved_user_modified` in the summary); only tooling files (slash commands, skills, hooks, `.mcp.json`) are refreshed.
Hook scripts and validator code live inside the installed `aexp` package; no Python you didn't write lands in your repo.
"""


def _print_actions(actions: list, *, dry_run: bool) -> None:
    kinds: dict[str, int] = {}
    for a in actions:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
        if a.kind == "skipped_conflict":
            console.print(f"[yellow]{a.kind}[/yellow] {a.path}: {a.detail}")
        elif a.kind == "preserved_user_modified":
            # Per-file line so users know exactly which scaffold files kept
            # their edits under --force.
            console.print(
                f"[cyan]preserved_user_modified[/cyan] {a.path}: "
                "kept your content; shipped default not applied"
            )
        elif a.kind == "gitignore_migration_warning":
            # Surface this prominently; the consumer's gitignore needs a
            # manual edit before `.aexp/runs-index/` and `.aexp/ledger/`
            # can be committed.
            console.print(f"[yellow]MIGRATION[/yellow] {a.path}: {a.detail}")
        elif a.kind in (
            "merged_json",
            "merged_block",
            "merged_gitignore",
            "wrote_marker",
            "initialized_runs",
        ):
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
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite conflicting tooling files (slash commands, skills, "
            "hooks, .mcp.json). User-authored scaffold content under `kb/` "
            "and `templates/` is preserved even here — delete the file "
            "first if you want to reset it to the shipped default."
        ),
    ),
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
    allow_self_install: bool = typer.Option(
        False,
        "--allow-self-install",
        help=(
            "Permit installing into the agentic-experiments source tree "
            "itself. Default is to refuse — running install inside the "
            "package's own source tree pollutes the dev repo and is "
            "almost always the result of a cwd mishap (e.g. `poetry -C "
            "<aexp-repo> run aexp install` from a scratch directory)."
        ),
    ),
    with_jupyter: bool = typer.Option(
        False,
        "--with-jupyter",
        help=(
            "Opt into the Jupyter MCP integration: writes the `jupyter` "
            "server entry to `.mcp.json`, sets "
            "`jupyter_enabled: true` (sticky) in the install marker, and "
            "ensures `docs/setup/jupyter-mcp.md` is copied in. Requires "
            "`pip install agentic-experiments[jupyter]` for the Python "
            "deps (jupyter-collaboration, jupyter-mcp-server, "
            "jupyter-mcp-tools). After install, follow the cluster-side "
            "recipe in docs/setup/jupyter-mcp.md (extension disables/"
            "enables via `aexp jupyter-setup`, stable token, sbatch "
            "script). The /aexp-jupyter-iterate slash command is "
            "installed regardless of this flag and self-checks for "
            "tool availability when invoked."
        ),
    ),
    machine_label: str = typer.Option(
        "",
        "--machine-label",
        help=(
            "Short identifier for this install, written to "
            "`.aexp/installed.json::machine_label`. Used by "
            "`aexp runs-export-index` and the Phase 2 ledger to tag which "
            "install registered a run. Empty string (default) keeps the "
            "previous marker's value if any, else falls back to short "
            "hostname (e.g. `kaden-thinkpad`). On HPC, pass an explicit "
            "name (`--machine-label cluster`) so per-node hostnames "
            "(`gpu023`, `h4huhnlogin2`) don't proliferate index files."
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
        try:
            actions = install_scaffold(
                cwd,
                run_store=run_store,
                force=force,
                assert_git=assert_git,
                dry_run=True,
                dev=dev,
                allow_self_install=allow_self_install,
                with_jupyter=with_jupyter,
                machine_label=machine_label or None,
            )
        except InstallRefused as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
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

    try:
        actions = install_scaffold(
            cwd,
            run_store=run_store,
            force=force,
            assert_git=assert_git,
            dev=dev,
            allow_self_install=allow_self_install,
            with_jupyter=with_jupyter,
            machine_label=machine_label or None,
        )
    except InstallRefused as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
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
    thread: str | None = typer.Option(
        None,
        "--thread",
        help=(
            "Optional T### parent thread this hypothesis was promoted from. "
            "When set, the thread must exist on disk; its ## Links will be "
            "auto-patched."
        ),
    ),
) -> None:
    """Create a new hypothesis (H###) with a validator-clean skeleton."""
    try:
        result = new_hypothesis(
            title=title,
            artifact_id=id,
            extra_links=link or None,
            thread_id=thread,
        )
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
# thread lifecycle (T### — research direction broader than a hypothesis)
# ---------------------------------------------------------------------------


@app.command("new-thread")
def new_thread_cmd(
    title: str = typer.Option(..., "--title", help="Human-readable thread title."),
    id: str | None = typer.Option(None, "--id", help="Force a specific T### id."),
    link: list[str] = typer.Option(
        [], "--link", help="Extra target to add to ## Links (repeatable)."
    ),
) -> None:
    """Create a new thread (T###) — a forward-looking research concern
    broader than a hypothesis.

    Threads capture exploration that may spawn 2-5 hypotheses over their
    lifetime. They're not in the H→E→F enforcement chain; they're parent
    context. Promote a thread to a hypothesis with
    ``aexp new-hypothesis --thread T###``. Close a thread with
    ``aexp close-thread T###``.
    """
    try:
        result = new_thread(
            title=title, artifact_id=id, extra_links=link or None
        )
    except ArtifactCreateError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
    _print_artifact_result("thread", result)


# ---------------------------------------------------------------------------
# sandbox scaffolding (notebooks/_sandbox/<date>_<slug>/ — exploratory work)
# ---------------------------------------------------------------------------


@app.command("new-sandbox")
def new_sandbox_cmd(
    slug: str = typer.Option(
        ...,
        "--slug",
        help=(
            "Filesystem-safe slug for the experiment "
            "(lowercase, hyphen-separated, alnum-only)."
        ),
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help=(
            "Human-readable title for the per-experiment README H1. "
            "Defaults to a Title-Cased version of the slug."
        ),
    ),
    parent_dir: str | None = typer.Option(
        None,
        "--parent-dir",
        help=(
            "Override the sandbox-root location. Defaults to "
            "<repo>/notebooks/_sandbox/. Relative paths resolve "
            "under the repo root."
        ),
    ),
) -> None:
    """Scaffold a new sandbox experiment subdirectory under `notebooks/_sandbox/`.

    A sandbox is exploratory free-form work. It's NOT a tracked research
    artifact (no H###/E### allocation, no kb_write_guard validation).
    Promote to the tracked-artifact graph with `/aexp-promote-nb` once
    a directional hypothesis lands.

    Creates `<parent_dir>/<YYYY-MM-DD>_<slug>/` populated with:
      - `README.md` — experiment-design template
      - `helpers.py` — sandbox-local utilities stub with repo-root
        boilerplate

    On first sandbox creation, also initializes the sandbox root
    (`notebooks/_sandbox/README.md` + `.gitignore` for large outputs).
    """
    try:
        result = scaffold_sandbox(
            slug=slug, title=title, parent_dir=parent_dir
        )
    except SandboxScaffoldError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
    console.print(
        f"[green]scaffolded[/green] sandbox [bold]{result.dir_name}[/bold]"
    )
    console.print(f"  dir: {result.dir_path}")
    for f in result.files_created:
        console.print(f"    + {f}")
    if result.root_initialized:
        console.print(
            "  [dim](also initialized sandbox root README + .gitignore)[/dim]"
        )


@app.command("list-threads")
def list_threads_cmd(
    status: str | None = typer.Option(
        None,
        "--status",
        help="Filter: PROPOSED | EXPLORING | PROMOTED | CLOSED.",
    ),
    tag: str | None = typer.Option(
        None, "--tag", help="Filter to threads with this tag in frontmatter."
    ),
) -> None:
    """List every thread in kb/research/threads/."""
    from aexp.utils.paths import find_repo_root

    kb = find_repo_root() / "kb"
    threads = list_kb_artifacts(kb, kind="T")
    rows = []
    for t in threads:
        t_status = str(t.metadata.get("Status", "") or "").strip()
        t_tags = t.metadata.get("tags") or []
        if status is not None and t_status != status:
            continue
        if tag is not None:
            tag_list = (
                t_tags if isinstance(t_tags, list) else [str(t_tags)]
            )
            if tag not in tag_list:
                continue
        rows.append((t.id, t_status, t.title, t.path))

    table = Table(title=f"threads ({len(rows)})", show_header=True)
    for col in ("id", "status", "title", "path"):
        table.add_column(col)
    for row in sorted(rows):
        table.add_row(*[str(c) for c in row])
    console.print(table)


@app.command("show-thread")
def show_thread_cmd(thread_id: str) -> None:
    """Show one thread's frontmatter + body summary."""
    from aexp.utils.paths import find_repo_root

    kb = find_repo_root() / "kb"
    try:
        t = load_thread(thread_id, kb_root=kb)
    except ArtifactNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
        return
    console.print(f"[bold]{t.id}[/bold] — {t.title}")
    console.print(f"  path: {t.path}")
    status = t.metadata.get("Status", "") or "(unset)"
    created = t.metadata.get("Created", "") or "(unset)"
    last_updated = t.metadata.get("Last updated", "") or "(unset)"
    console.print(f"  status: [cyan]{status}[/cyan]")
    console.print(f"  created: {created}")
    console.print(f"  last_updated: {last_updated}")


@app.command("close-thread")
def close_thread_cmd(
    thread_id: str,
    conclusion: str | None = typer.Option(
        None,
        "--conclusion",
        help=(
            "Markdown body to write into the thread's ## Conclusion "
            "section. If omitted, existing body is preserved."
        ),
    ),
    promoted: bool = typer.Option(
        False,
        "--promoted",
        help=(
            "Set status to PROMOTED instead of CLOSED. Use when one or "
            "more hypotheses have been spawned and the thread persists "
            "as parent context."
        ),
    ),
) -> None:
    """Transition a thread to CLOSED (default) or PROMOTED."""
    target_status = "PROMOTED" if promoted else "CLOSED"
    try:
        result = close_thread(
            thread_id, conclusion=conclusion, new_status=target_status
        )
    except ArtifactCreateError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
        return
    console.print(
        f"[green]{result.new_status}[/green] [bold]{result.thread_id}[/bold]"
    )
    console.print(f"  path: {result.path}")
    if result.conclusion_written:
        console.print("  conclusion: rewritten")


# ---------------------------------------------------------------------------
# run creation + browsing
# ---------------------------------------------------------------------------


@app.command("new-run")
def new_run(
    experiment: str = typer.Option(..., "--experiment", help="E### id."),
    hypothesis: str | None = typer.Option(None, "--hypothesis"),
    sub_hypothesis: str | None = typer.Option(None, "--sub-hypothesis"),
    sp: str | None = typer.Option(None, "--sp", help="KEY=VAL,KEY=VAL state-point params."),
    no_commit: bool = typer.Option(False, "--no-commit", help="Skip code_commit/code_dirty in sp."),
) -> None:
    """Create a signac job linked to an experiment."""
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
    """List signac jobs filtered by run-link + sp."""
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
    """Show state point, doc, linked research frame for a run."""
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
    """Retroactively stamp ``job.doc['aexp']`` onto an existing run."""
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
    strict_runs: str = typer.Option(
        "error",
        "--strict-runs",
        help=(
            "Severity for finding-citation existence checks. "
            "'error' (default): broken/empty citations exit 1. "
            "'warn': downgrade to warnings, exit 0. "
            "'off': skip existence checks entirely (structural checks still run). "
            "Use 'warn' on a laptop when runs live on a cluster you can't see "
            "locally; Phase 1B's `aexp runs-export-index` removes the need by "
            "letting the validator distinguish 'elsewhere' from 'broken'."
        ),
    ),
) -> None:
    """Validate the KB + run-link integrity."""
    if kb_only and runs_only:
        console.print("[red]cannot combine --kb-only and --runs-only[/red]")
        _exit(2)
    if strict_runs not in ("error", "warn", "off"):
        console.print(
            f"[red]invalid --strict-runs={strict_runs!r}; "
            "expected one of: error, warn, off[/red]"
        )
        _exit(2)
    mode = "kb-only" if kb_only else ("runs-only" if runs_only else "full")
    result: ValidateResult = validate_repo(
        mode=mode,  # type: ignore[arg-type]
        strict_runs=strict_runs,  # type: ignore[arg-type]
    )
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
# runs-export-index — Phase 1B
# ---------------------------------------------------------------------------


@app.command("runs-export-index")
def runs_export_index_cmd(
    machine_label: str = typer.Option(
        "",
        "--as",
        "--machine-label",
        help=(
            "Override the machine label for this export. Defaults to "
            "`installed.json::machine_label` (typically short hostname). "
            "Pass a stable name on HPC where per-node hostnames are noisy."
        ),
    ),
    out: str = typer.Option(
        "",
        "--out",
        help=(
            "Output file path. Defaults to "
            "`<repo>/.aexp/runs-index/<machine_label>.json`."
        ),
    ),
) -> None:
    """Export this machine's terminal-state runs to a JSON index file.

    Each machine periodically runs this verb and commits the resulting
    file. Other machines pull and the validator unions the indexes to
    distinguish "registered elsewhere" (warning) from "broken" (error)
    citations.

    Phase 1B mechanism — superseded by `aexp ledger backfill` once
    Phase 2's universal ledger lands. Deprecation window: one minor
    version after Phase 2 ships.
    """
    from aexp.runs_index import export_index
    from aexp.utils.paths import find_repo_root

    root = find_repo_root()
    target = Path(out).resolve() if out else None
    label_override = machine_label or None

    path = export_index(root, out=target, machine_label=label_override)
    # Count entries for the summary line.
    import json as _json
    try:
        entries = _json.loads(path.read_text(encoding="utf-8")).get("entries", [])
        n = len(entries)
    except Exception:
        n = -1
    console.print(f"[green][OK][/green] wrote {n} terminal-run entries to {path}")


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


# ---------------------------------------------------------------------------
# jupyter subcommand group — live session introspection + extension setup
# ---------------------------------------------------------------------------


jupyter_app = typer.Typer(
    help=(
        "Live Jupyter session introspection (whoami, discover) plus the "
        "extension-disable/enable recipe needed before the Jupyter MCP can "
        "talk to a cluster JupyterLab."
    ),
    no_args_is_help=True,
)
app.add_typer(jupyter_app, name="jupyter")


def _do_jupyter_setup(dry_run: bool) -> None:
    """Idempotent Jupyter extension reconfiguration.

    Body shared between ``aexp jupyter setup`` (the canonical verb) and the
    deprecated flat ``aexp jupyter-setup`` alias.
    """
    import subprocess
    import sys as _sys

    cmds: list[list[str]] = [
        [_sys.executable, "-m", "jupyter", "server", "extension", "disable", "jupyter_server_documents"],
        [_sys.executable, "-m", "jupyter", "server", "extension", "enable", "jupyter_server_ydoc"],
        [_sys.executable, "-m", "jupyter", "server", "extension", "enable", "jupyter_server_nbmodel"],
        [_sys.executable, "-m", "jupyter", "labextension", "disable", "@jupyter-ai-contrib/server-documents"],
    ]

    for cmd in cmds:
        printable = " ".join(cmd)
        if dry_run:
            console.print(f"[cyan][dry-run][/cyan] {printable}")
            continue
        console.print(f"[dim]$[/dim] {printable}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            console.print(result.stdout.rstrip())
        if result.returncode != 0:
            console.print(f"[red]FAILED ({result.returncode})[/red] {result.stderr.rstrip()}")
            _exit(1)
            return

    if dry_run:
        console.print("\n[cyan]dry-run complete[/cyan] — no changes applied.")
        return
    console.print("\n[green]✓[/green] Jupyter extension state configured.")
    console.print("  Restart your JupyterLab process to pick up the changes.")


@jupyter_app.command("setup")
def jupyter_setup_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Print the commands that would be run without executing them.",
    ),
) -> None:
    """Configure Jupyter Server extensions for the Jupyter MCP integration.

    Run this on the cluster (login node, inside the conda env / venv that
    hosts your `jupyter lab`). Idempotent — safe to re-run after a fresh
    install pulls in conflicting Datalayer extensions.

    The four operations applied:

    \b
      - DISABLE jupyter_server_documents          (server extension; replaces
                                                   kernel manager and breaks
                                                   the MCP kernel-WS path)
      - DISABLE @jupyter-ai-contrib/server-documents
                                                  (lab frontend extension that
                                                   keeps calling Datalayer
                                                   private routes after the
                                                   server half is disabled →
                                                   "File ID error" on every
                                                   notebook open)
      - ENABLE  jupyter_server_ydoc               (provides /api/collaboration
                                                   routes; some envs ship it
                                                   disabled)
      - ENABLE  jupyter_server_nbmodel            (required for `execute_cell`
                                                   in JUPYTER_SERVER mode)

    See `docs/setup/jupyter-mcp.md` "Investigation log" §1-3 for the full
    rationale. Restart your JupyterLab process to pick up the changes.
    """
    _do_jupyter_setup(dry_run)


@app.command("jupyter-setup", hidden=True)
def jupyter_setup_deprecated_alias(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Print the commands that would be run without executing them.",
    ),
) -> None:
    """Deprecated alias for ``aexp jupyter setup``. Kept for one release."""
    console.print(
        "[yellow]warning[/yellow] `aexp jupyter-setup` is deprecated; use "
        "`aexp jupyter setup` (subcommand) instead."
    )
    _do_jupyter_setup(dry_run)


def _print_session_info(json_out: bool) -> None:
    """Shared body for ``aexp jupyter init`` / ``aexp jupyter whoami``."""
    try:
        from aexp.jupyter import _print_info_human, init
    except ImportError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(1)
        return
    info = init()
    if json_out:
        # model_dump_json is the wire format the MCP recipe also produces.
        print(info.model_dump_json(indent=2))
        return
    _print_info_human(info)


@jupyter_app.command("init")
def jupyter_init_cmd(
    json_out: bool = typer.Option(
        False, "--json", help="Emit SessionInfo as JSON instead of human-readable text."
    ),
) -> None:
    """Introspect the current Jupyter session and print the result.

    Most useful when run from inside a Jupyter terminal or a notebook cell
    (``!aexp jupyter init``) — populates SLURM context, attached
    notebooks, GPU residents, and sibling Jupyters from live state.
    """
    _print_session_info(json_out)


@jupyter_app.command("whoami")
def jupyter_whoami_cmd(
    json_out: bool = typer.Option(
        False, "--json", help="Emit SessionInfo as JSON instead of human-readable text."
    ),
) -> None:
    """Alias for ``aexp jupyter init`` — identical output, friendlier verb."""
    _print_session_info(json_out)


@jupyter_app.command("discover")
def jupyter_discover_cmd(
    json_out: bool = typer.Option(
        False, "--json", help="Emit the sibling list as JSON instead of a table."
    ),
    describe: bool = typer.Option(
        False,
        "--describe",
        help=(
            "Probe each sibling's /api/sessions and /api/kernels for attached "
            "notebook paths + kernel state (one HTTP roundtrip per server)."
        ),
    ),
) -> None:
    """List every Jupyter the user has running, excluding the current one."""
    try:
        from aexp.jupyter import describe_server, discover_other_servers
    except ImportError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(1)
        return
    siblings = discover_other_servers()
    if describe:
        rendered = []
        for s in siblings:
            entry = s.model_dump()
            entry["describe"] = describe_server(s.url, s.token)
            rendered.append(entry)
        if json_out:
            import json as _json
            print(_json.dumps(rendered, indent=2, default=str))
            return
        for entry in rendered:
            console.print(
                f"[cyan]{entry['url']}[/cyan]  port={entry['port']}  "
                f"pid={entry['pid']}  host={entry['hostname']}"
            )
            d = entry["describe"]
            if d.get("attached_notebooks"):
                console.print("  notebooks:")
                for nb in d["attached_notebooks"]:
                    console.print(f"    - {nb}")
            for k in d.get("kernels", []):
                console.print(
                    f"  kernel: id={k.get('id')} state={k.get('execution_state')} "
                    f"last={k.get('last_activity')}"
                )
        if not rendered:
            console.print("[dim]no other Jupyter servers visible[/dim]")
        return

    if json_out:
        import json as _json
        print(_json.dumps([s.model_dump() for s in siblings], indent=2, default=str))
        return
    if not siblings:
        console.print("[dim]no other Jupyter servers visible[/dim]")
        return
    for s in siblings:
        console.print(
            f"[cyan]{s.url}[/cyan]  port={s.port}  pid={s.pid}  host={s.hostname}"
        )


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

# The airgapped-relay subcommand group is defined in aexp.airgapped._relay
# (alongside the request() core it drives). Register it here so it is
# reachable as `aexp airgapped ...`; it is also `python -m aexp.airgapped`.
app.add_typer(airgapped_app, name="airgapped")


# ---------------------------------------------------------------------------
# ledger subcommand group — Phase 2 universal cross-machine ledger
# ---------------------------------------------------------------------------


ledger_app = typer.Typer(
    help=(
        "Universal cross-machine ledger: sanitized projections of terminal "
        "runs at .aexp/ledger/<job_id>.json. Committed to git so every "
        "machine sees the same view after `git pull`."
    ),
    no_args_is_help=True,
)
app.add_typer(ledger_app, name="ledger")


@ledger_app.command("promote")
def ledger_promote_cmd(
    job_id: str = typer.Argument(..., help="32-hex job id to promote."),
    machine_label: str = typer.Option(
        "",
        "--machine-label",
        help=(
            "Override registered_machine in the ledger entry. Defaults to "
            "`installed.json::machine_label`."
        ),
    ),
) -> None:
    """Manually promote a terminal-state job to the ledger.

    For cases where the auto-hook in mark_status() didn't fire (rare —
    out-of-band status writes, pre-Phase-2 runs). Idempotent.
    """
    from aexp.ledger import promote_to_ledger
    from aexp.runs import open_run
    from aexp.utils.paths import find_repo_root

    root = find_repo_root()
    job = open_run(job_id, repo_root=root)
    path = promote_to_ledger(
        job, repo_root=root, machine_label=(machine_label or None)
    )
    console.print(f"[green][OK][/green] promoted {job_id} -> {path}")


@ledger_app.command("backfill")
def ledger_backfill_cmd(
    machine_label: str = typer.Option(
        "",
        "--machine-label",
        help=(
            "Tag every backfilled entry with this label. Defaults to "
            "`installed.json::machine_label`."
        ),
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help=(
            "Re-promote even already-present entries. Useful after "
            "bumping the schema version or a bulk `aexp link` fix."
        ),
    ),
) -> None:
    """Promote every terminal-state local run to the ledger.

    The Phase 2 migration verb. Each machine with terminal-state runs
    runs this once after upgrading to 0.6; commits the resulting
    `.aexp/ledger/<id>.json` files; pushes. After every machine has
    pushed, every machine pulls and the validator resolves all cited
    jobs universally.
    """
    from aexp.ledger import backfill_ledger
    from aexp.utils.paths import find_repo_root

    root = find_repo_root()
    promoted, skipped = backfill_ledger(
        root, machine_label=(machine_label or None), overwrite=overwrite
    )
    console.print(
        f"[green][OK][/green] promoted {promoted} entries "
        f"({skipped} already present, skipped)"
    )


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
    experiment: str = typer.Option(..., "--experiment", help="E### id."),
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
    allow_dup_on_recommit: bool = typer.Option(
        False,
        "--allow-dup-on-recommit",
        help=(
            "Add the new entry even if a pending entry with the same sp "
            "modulo code_commit/code_dirty already exists. Without this "
            "flag (default), recommit duplicates are skipped with a "
            "warning."
        ),
    ),
) -> None:
    """Register one or more pending runs.

    Pending entries that match an existing pending entry's sp modulo
    `code_commit`/`code_dirty` (i.e. the only diff is the auto-captured
    commit hash) are skipped by default with a warning — pass
    `--allow-dup-on-recommit` to insert them anyway.
    """
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
        with _capture_dup_warnings() as captured:
            jobs = add_many_to_queue(
                experiment_id=experiment,
                hypothesis_id=hypothesis,
                base_sp=base_sp,
                sweep=sweep_dict,
                tag=tag,
                runner_hint=runner_hint,
                include_commit=not no_commit,
                resolve_conditions=not no_resolve,
                allow_dup_on_recommit=allow_dup_on_recommit,
            )
        if captured:
            console.print(
                f"[yellow]skipped {len(captured)} recommit duplicate(s); "
                "rerun with --allow-dup-on-recommit to add them.[/yellow]"
            )
        console.print(
            f"[green]queued[/green] [bold]{len(jobs)}[/bold] job(s)"
            + (f" under tag=[cyan]{tag}[/cyan]" if tag else "")
        )
        for job in jobs:
            console.print(f"  {job.id[:8]}  {dict(job.sp)}")
        return

    with _capture_dup_warnings() as captured:
        job = add_to_queue(
            experiment_id=experiment,
            hypothesis_id=hypothesis,
            statepoint=base_sp,
            tag=tag,
            runner_hint=runner_hint,
            include_commit=not no_commit,
            resolve_conditions=not no_resolve,
            allow_dup_on_recommit=allow_dup_on_recommit,
        )
    if captured:
        console.print(
            f"[yellow]skipped — recommit duplicate of pending {job.id[:8]}. "
            "rerun with --allow-dup-on-recommit to add anyway.[/yellow]"
        )
        return
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


@queue_app.command("stop")
def queue_stop_cmd(
    job_id: str,
    grace_s: float = typer.Option(
        5.0,
        "--grace-s",
        help=(
            "Seconds to wait between SIGTERM and SIGKILL. Set to 0 to "
            "skip the grace period and SIGKILL immediately."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip SIGTERM, send SIGKILL straight away.",
    ),
) -> None:
    """Interrupt a running queued job (the live `aexp run-queued <id>` subprocess).

    Reads the live-process pointer that ``run_queued`` writes into the
    job doc, sends SIGTERM (or SIGKILL with ``--force``), and transitions
    the job's status to ``stopped`` — distinct from ``failed`` (runtime
    crash) and ``abandoned`` (never ran).

    Refuses when the recorded host is not this machine; ssh into the
    recording host and rerun there. PID-recycle is detected via
    process-start-time fingerprints on Linux; on other platforms a small
    race window remains and is documented.
    """
    try:
        stop_queued(job_id, grace_s=grace_s, force=force)
    except StopJobError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
        return
    console.print(f"[yellow]stopped[/yellow] {job_id[:8]}")


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


@queue_app.command("run")
def queue_run_cmd(
    experiment: str | None = typer.Option(None, "--experiment"),
    tag: str | None = typer.Option(None, "--tag"),
    index: int | None = typer.Option(
        None,
        "--index",
        help=(
            "If set, run only the Nth pending job (0-indexed). Intended "
            'for slurm array tasks: `--index "$SLURM_ARRAY_TASK_ID"`. '
            "Without --index, runs every pending job in the filter "
            "sequentially."
        ),
    ),
    continue_on_failure: bool = typer.Option(
        False,
        "--continue-on-failure",
        help=(
            "When running multiple jobs without --index, don't bail on the "
            "first failure — keep going. Default: stop on first failure."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run jobs in terminal states."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print rendered commands without executing."
    ),
) -> None:
    """Execute queued jobs from inside your own batch script.

    Iterates the pending queue (filtered by --experiment / --tag) and
    runs each matching job via `aexp run-queued` semantics. Designed to
    live inside whatever batch script already works for your site:

    \b
      # Sequential (single-node):
      aexp queue run --tag overnight

    \b
      # Array-parallel (one queued job per slurm array task):
      #SBATCH --array=0-7
      aexp queue run --tag overnight --index "$SLURM_ARRAY_TASK_ID"

    Jobs are enumerated in stable order (ascending queued_at, then
    job_id) so --index picks deterministically.
    """
    try:
        returncodes = run_queue(
            experiment_id=experiment,
            tag=tag,
            index=index,
            continue_on_failure=continue_on_failure,
            force=force,
            dry_run=dry_run,
        )
    except IndexError as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
        return
    except RunnerCommandMissing as exc:
        console.print(f"[red]{exc}[/red]")
        _exit(2)
        return
    except SubprocessFailed as exc:
        console.print(f"[red]runner failed: {exc}[/red]")
        _exit(1)
        return

    if not returncodes:
        console.print("[yellow]no queued jobs match the filter[/yellow]")
        return
    failed = sum(1 for rc in returncodes if rc != 0)
    passed = len(returncodes) - failed
    if failed == 0:
        console.print(
            f"[green]ran {passed}/{len(returncodes)} job(s) successfully[/green]"
        )
    else:
        console.print(
            f"[yellow]ran {len(returncodes)} job(s), "
            f"{failed} failed / {passed} passed[/yellow]"
        )
        _exit(1)


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
