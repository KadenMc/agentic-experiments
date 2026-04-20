"""Typer CLI — ``aex`` / ``agentic-experiments`` entry point.

Verbs are stubbed where implementation is pending; shape tracks plan §9.
"""
from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(
    name="aex",
    help="Agentic Experiments — Limina + signac + W&B fusion layer.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


def _not_implemented(verb: str) -> None:
    """Uniform stub message until the verb lands."""
    typer.secho(
        f"[aex {verb}] not implemented yet — see plan §9.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print package version."""
    from agentic_experiments import __version__
    typer.echo(__version__)


@app.command()
def install(
    run_store: str = typer.Option(".runs", "--run-store", help="Path for signac project."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Install the vendored Limina harness into the current git repo."""
    _not_implemented("install")


@app.command("new-run")
def new_run(
    experiment: str = typer.Option(..., "--experiment", help="Limina E### id."),
    hypothesis: Optional[str] = typer.Option(None, "--hypothesis"),
    sp: Optional[str] = typer.Option(None, "--sp", help="KEY=VAL,KEY=VAL state-point params."),
    no_commit: bool = typer.Option(False, "--no-commit"),
) -> None:
    """Create a signac job linked to a Limina experiment."""
    _not_implemented("new-run")


@app.command("list-runs")
def list_runs(
    experiment: Optional[str] = typer.Option(None, "--experiment"),
    hypothesis: Optional[str] = typer.Option(None, "--hypothesis"),
    status: Optional[str] = typer.Option(None, "--status"),
    sp: Optional[str] = typer.Option(None, "--sp"),
) -> None:
    """List signac jobs filtered by experiment / hypothesis / status / sp."""
    _not_implemented("list-runs")


@app.command("list-batches")
def list_batches(
    experiment: Optional[str] = typer.Option(None, "--experiment"),
) -> None:
    """List distinct (experiment, condition, ...) slices over runs."""
    _not_implemented("list-batches")


@app.command("show-run")
def show_run(job_id: str) -> None:
    """Show state point, doc, linked Limina frame for a run."""
    _not_implemented("show-run")


@app.command("show-batch")
def show_batch(
    experiment: str = typer.Option(..., "--experiment"),
    condition: str = typer.Option(..., "--condition"),
    model: Optional[str] = typer.Option(None, "--model"),
) -> None:
    """Aggregate view over a batch slice."""
    _not_implemented("show-batch")


@app.command()
def link(
    job_id: str,
    experiment: str = typer.Option(..., "--experiment"),
) -> None:
    """Link an existing run to a Limina experiment."""
    _not_implemented("link")


@app.command("bind-tracker")
def bind_tracker(
    job_id: str,
    backend: str = typer.Option("noop", "--backend", help="noop | wandb"),
    project: Optional[str] = typer.Option(None, "--project"),
    offline: bool = typer.Option(False, "--offline"),
) -> None:
    """Attach a tracker run (e.g. W&B) to an existing signac job."""
    _not_implemented("bind-tracker")


@app.command()
def validate(
    kb_only: bool = typer.Option(False, "--kb-only"),
    runs_only: bool = typer.Option(False, "--runs-only"),
) -> None:
    """Validate Limina KB + run-link integrity."""
    _not_implemented("validate")


@app.command("install-slash-commands")
def install_slash_commands(
    target: str = typer.Option(".claude/commands", "--target"),
) -> None:
    """Copy shipped slash commands into .claude/commands/."""
    _not_implemented("install-slash-commands")


if __name__ == "__main__":
    app()
