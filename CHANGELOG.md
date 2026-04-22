# Changelog

All notable changes to `agentic-experiments` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial v1 of the Limina-fork + signac + W&B fusion layer.
- `aexp install` applies a vendored Limina snapshot to a consumer repo:
  copies `kb/`, `templates/`, `scripts/` (with four shell hooks ported to
  cross-platform Python), JSON-merges `.claude/settings.json`, block-merges
  `AGENTS.md`/`CLAUDE.md`, and initializes a signac project at `.runs/`.
  Also copies the four Limina research skills (`experiment-rigor`,
  `exploratory-sota-research`, `research-devil-advocate`,
  `build-maintainable-software`) into `.claude/skills/`.
- `aexp` Typer CLI: `install`, `new-run`, `list-runs`, `list-batches`,
  `show-run`, `show-batch`, `link`, `bind-tracker`, `sync-offline`,
  `validate`, `install-slash-commands`.
- Python API re-exported from `aexp`: `install_limina`, `create_run`,
  `open_run`, `find_runs`, `run_lifecycle`, `list_batches`, `show_batch`,
  `link_to_experiment`, `load_hypothesis` / `load_experiment` /
  `load_finding`, `bind_tracker`, `NoopAdapter`, `WandbAdapter`,
  `validate_repo`.
- Tracker adapters: `NoopAdapter` (always on; writes JSONL to
  `<workspace>/tracker_log/`) and `WandbAdapter` (optional `[wandb]`
  extra; co-locates offline-run dirs with the signac workspace).
- HPC workflow: `aexp sync-offline` walks `.runs/workspace/*/wandb/` and
  calls `wandb sync` on every offline run — pair with `--offline` at
  `bind-tracker` time on compute nodes.
- MCP server (`aexp.mcp_server`, behind optional `[mcp]` extra) exposes
  the Python API as typed tools for Claude Code. `aexp install` wires
  the server into `.claude/settings.json` so Claude picks it up
  automatically.
- Cross-platform invocation: `.aexp/installed.json` records `python_exe`
  and `conda_env_name` at install time; `aexp.utils.resolve_invocation`
  returns the correct argv prefix from any shell context.
- Validator (`aexp validate`): composes vendored `kb_validate.py`
  (structural) with run-link / finding-citation integrity checks.
  Error codes: `limina.validation_failed`, `run.orphan`,
  `run.broken_experiment_link`, `run.hypothesis_mismatch`,
  `run.sub_hypothesis_unlisted`, `run.status_invalid`,
  `finding.broken_run_citation`, `finding.empty_batch`.
- Slash commands (`/aexp-new-run`, `/aexp-close-run`, `/aexp-close-batch`)
  installed via `aexp install-slash-commands`.

[Unreleased]: https://github.com/kadenmc/agentic-experiments/compare/v0.0.1...HEAD
