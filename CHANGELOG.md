# Changelog

All notable changes to `agentic-experiments` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Artifact-creation API** (`aexp.artifacts`): `new_hypothesis`,
  `new_experiment`, `new_finding`. Each allocates the smallest unused
  `H###` / `E###` / `F###` id, renders the shipped template (preferring
  `<repo>/templates/<kind>.md` when the consumer repo has a local override),
  writes the file, and — for experiments and findings — patches every
  parent artifact's `## Links` section via `aexp.backlinks.add_backlink`
  so `kb_validate`'s bidirectional-link check passes on the first write.
- **Backlink helper** (`aexp.backlinks.add_backlink`): idempotent
  ``## Links`` section patcher that tolerates anchored (`[[X#sec]]`) and
  aliased (`[[X|alt]]`) wikilinks and creates a ``## Links`` section at
  end of file when one is missing.
- **New CLI verbs**: `aexp new-hypothesis`, `aexp new-experiment`,
  `aexp new-finding` — thin wrappers over the Python API with rich
  console output reporting which parent files were patched.
- **New MCP tools**: `new_hypothesis`, `new_experiment`, `new_finding` —
  same surface as the CLI verbs, returning typed dicts.
- **New slash commands**: `/aexp-new-hypothesis`, `/aexp-new-experiment`,
  `/aexp-new-finding`, `/aexp-status`, `/aexp-list-runs`, `/aexp-validate`.
  Six commands added on top of the existing three; install copies all of
  them into `.claude/commands/`.

### Changed

- **`SessionStart` hook** no longer emits `=== WARNING: <file> not found ===`
  when `kb/ACTIVE.md` or `kb/mission/CHALLENGE.md` is absent. Consumer repos
  may legitimately defer authoring `CHALLENGE.md` until they have a real
  mission statement; `aexp validate` still surfaces the absence as a
  structured `filesystem` issue when it actually matters.

## [0.1.1] — 2026-04-22

### Added

- **`aexp install --dev` / `-D`** — writes a development-mode `.mcp.json`
  whose `aexp` entry invokes the current Python interpreter directly
  (``"<python_exe>" -m aexp.mcp_server``) instead of the default
  portable `uvx --from agentic-experiments[mcp]` form. Intended for
  maintainers editing `aexp` locally via `pip install -e` who want
  source edits to flow through to the MCP surface; the resulting
  `.mcp.json` bakes in a machine-specific path and should not be
  committed.
- Accompanying advisory printed whenever `--dev` is set, including
  under `--yes`, so users don't accidentally commit a dev-form
  `.mcp.json`.

### Docs
- `docs/mcp.md`, `docs/cli.md`, and the install CLI help text describe
  `--dev` and the commit / gitignore implications.

## [0.1.0] — 2026-04-22

First public release — hypothesis-first experiment tracking for agent-driven
ML research, wired into Claude Code.

### Added

#### Core package (`aexp`)
- **Typer CLI** (`aexp <verb>`) with ten verbs: `install`, `new-run`,
  `list-runs`, `list-batches`, `show-run`, `show-batch`, `link`,
  `bind-tracker`, `sync-offline`, `validate`, `install-slash-commands`.
- **Python API** re-exported from the top-level package: `install_limina`,
  `create_run`, `open_run`, `find_runs`, `run_lifecycle`, `list_batches`,
  `show_batch`, `link_to_experiment`, `load_hypothesis` / `load_experiment` /
  `load_finding`, `bind_tracker`, `NoopAdapter`, `WandbAdapter`,
  `validate_repo`.
- **Typed schema** (`aexp.schema`) backed by pydantic: `RunLink`,
  `BatchSelector`, `BatchSummary`, `Issue`, `RunSummary`, etc.

#### Limina research harness
- `aexp install` applies the vendored Limina snapshot to a consumer repo:
  copies `kb/` scaffold + `templates/`, JSON-merges `.claude/settings.json`
  and `.mcp.json`, block-merges `AGENTS.md` / `CLAUDE.md`, initialises a
  signac project at `.runs/`, and records the install-time interpreter
  in `.aexp/installed.json`.
- Copies the four Limina research skills (`experiment-rigor`,
  `exploratory-sota-research`, `research-devil-advocate`,
  `build-maintainable-software`) into `.claude/skills/`.
- **Install UX**: prints a heads-up listing every file the install will
  touch and its merge policy, then prompts for confirmation.
  `--dry-run` / `-n` previews the plan with zero side effects.
  `--yes` / `-y` skips the prompt for scripted / CI use.
  Non-interactive stdin without `--yes` or `--dry-run` aborts cleanly.
- **`aexp install` respects existing content**: conflicting files are
  skipped by default (pass `--force` to overwrite). JSON merges preserve
  the user's own hooks, permissions, and MCP servers. Markdown merges
  only touch content between `<!-- agentic-experiments:begin/end -->`
  markers.

#### Claude Code hooks (in-package)
- `aexp.hooks` ships the four Claude Code hooks as Python modules:
  `session_start`, `enforce_hef_chain`, `kb_write_guard`, `stop_validate`.
- `.claude/settings.json` is generated with commands of the form
  `"<python_exe>" -m aexp.hooks.<name>`, pinned to the install-time
  interpreter. Hooks upgrade through `pip install -U agentic-experiments`
  rather than by re-running `aexp install`.
- **No Python code lands in the consumer repo** other than files the
  user writes themselves. Hooks, validator logic, and helpers all live
  in the installed package.

#### KB structural validation
- `aexp.kb_validate.validate_kb()` — pure in-process validator; covers
  frontmatter, aliases, wikilinks, bidirectional backlinks, and the
  H → E → F chain.
- `aexp.validate.validate_repo()` composes structural validation with
  run-link and finding-citation integrity. Issue codes:
  `limina.validation_failed`, `run.orphan`, `run.broken_experiment_link`,
  `run.hypothesis_mismatch`, `run.sub_hypothesis_unlisted`,
  `run.status_invalid`, `finding.broken_run_citation`,
  `finding.empty_batch`.

#### Tracker adapters
- `TrackerAdapter` ABC with two reference implementations:
  - `NoopAdapter` (always on; writes JSONL to `<workspace>/tracker_log/`)
  - `WandbAdapter` (optional `[wandb]` extra; co-locates offline-run
    dirs with the signac workspace)
- **HPC workflow**: `aexp sync-offline` walks `.runs/workspace/*/wandb/`
  and calls `wandb sync` on every offline run from a login node.

#### MCP server
- `aexp-mcp-server` entry point (optional `[mcp]` extra) exposes nine
  typed tools over FastMCP: `new_run`, `list_runs`, `list_batches`,
  `show_run`, `show_batch`, `link_run`, `bind_tracker`, `validate`,
  `sync_offline`.
- `aexp install` writes an `.mcp.json` at the repo root that invokes the
  server via `uvx --from agentic-experiments[mcp] aexp-mcp-server` —
  portable across machines, committable to git, no absolute paths, no
  per-teammate configuration.

#### Slash commands
- `/aexp-new-run`, `/aexp-close-run`, `/aexp-close-batch` installed into
  `.claude/commands/` via `aexp install-slash-commands`.

### Packaging
- **Python 3.11+** (3.11, 3.12, 3.13 all tested). CI runs on Ubuntu and
  Windows against every supported interpreter.
- `src/` layout; Poetry-built wheel + sdist; PyPA-compliant.
- Optional extras: `[wandb]` (tracker adapter), `[mcp]` (MCP server).
- 172 tests on the full suite; `tests/test_e2e_smoke.py` covers the
  full happy path end to end (install → H + E + runs → finding →
  validate → broken-link detection → re-run at new commit).

[Unreleased]: https://github.com/KadenMc/agentic-experiments/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/KadenMc/agentic-experiments/releases/tag/v0.1.1
[0.1.0]: https://github.com/KadenMc/agentic-experiments/releases/tag/v0.1.0
