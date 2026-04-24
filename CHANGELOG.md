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
  `/aexp-new-finding` (later renamed to `/aexp-finding-placeholder`,
  see "Changed" below), `/aexp-status`, `/aexp-list-runs`,
  `/aexp-validate`. Install copies them into `.claude/commands/`.

### Changed

- **`SessionStart` hook** no longer emits `=== WARNING: <file> not found ===`
  when `kb/ACTIVE.md` or `kb/mission/CHALLENGE.md` is absent. Consumer repos
  may legitimately defer authoring `CHALLENGE.md` until they have a real
  mission statement; `aexp validate` still surfaces the absence as a
  structured `filesystem` issue when it actually matters.

### Added (tracker redesign)

- **`aexp.tracked_run(job, *, project, ...)`** — context-manager entry
  point for managed wandb runs. aexp calls `wandb.init(**init_kwargs)` with
  the disciplined payload (deterministic group slug, auto-tags, curated
  Limina frame, flattened state point, workspace co-location), stamps
  `job.doc["tracker"]`, yields the live `wandb.Run`, and calls
  `run.finish(exit_code=...)` on exit. Full wandb API (`run.log_artifact`,
  `wandb.Table`, `run.define_metric`, `run.summary[...]`, sweeps) is
  available on the yielded run — aexp is not a wrapper.
- **`aexp.prepare_tracker(job, *, project, ...)`** — returns a
  `TrackerContext` with wandb-shaped `init_kwargs` so callers who already
  own `wandb.init` (e.g. per-item inference loops with custom names /
  artifacts) can splat the aexp payload into their own call, then stamp
  the signac binding via `ctx.bind(run)`. This is the "bring your own
  init" path — closes the duplicate-run-tree problem that consumers with
  pre-existing `wandb.init` sites hit with the adapter path.
- **`aexp.TrackerContext`** — frozen dataclass exported at the package
  root. `ctx.init_kwargs` holds the ready-to-splat payload; `ctx.group`
  / `ctx.project` / `ctx.tags` are mirrored for inspection; `ctx.bind(
  run, *, backend="wandb")` writes `TrackerBinding` into
  `job.doc["tracker"]` by duck-typing `run.id` / `run.url`.

### Changed (tracker redesign)

- **`docs/tracker-adapters.md`** rewritten. New framing: three modes
  documented side-by-side in a comparison table (Managed / BYO-init /
  Adapter). `tracked_run` and `prepare_tracker` lead; the
  `TrackerAdapter` ABC is documented as the noop / backend-agnostic path.
- **`bind_tracker(job, adapter, ...)`** signature and side effects
  unchanged; implementation now delegates to a shared
  `_derive_tracker_payload` helper used by both the adapter path and
  `prepare_tracker`. All existing tests (7 noop + 9 wandb + 3 CLI/MCP
  integration) pass unchanged.
- **`docs/quickstart.md`** — replaced the `bind_tracker(job,
  NoopAdapter(), ...)` Python example with parallel snippets for
  `tracked_run`, `prepare_tracker`, and the noop adapter path. Also
  removed the stale "v1.1 roadmap" note — the `new-hypothesis` /
  `new-experiment` / `new-finding` CLI verbs shipped.
- **`README.md`** — tracker row points at `tracked_run` /
  `prepare_tracker`; CLI row updated from "10 verbs" to "13 verbs"; slash
  commands row enumerates the full 9-command set; v1.1 backlog drops the
  items that already shipped.

### Added (slash-command UX cleanup)

- **`/aexp-show-run`** — guided read-only display of one signac run's
  state point, doc, and linked Limina frame. Slash-parity with
  `/aexp-list-runs`.
- **`/aexp-show-batch`** — guided read-only display of every run
  matching an `(experiment, condition)` batch selector, with status-mix
  summary. Useful pre-finding sanity check.

### Changed (slash-command UX cleanup)

- **Finding-creation slash commands renamed** to a parallel
  `aexp-finding-<source>` pattern, so the distinguishing dimension (what
  the finding cites) is self-explaining from the name rather than
  doc-text. The old "new / close" split conflated lifecycle framing with
  source-of-citation:
  - `/aexp-close-run` → `/aexp-finding-from-run` (one specific job)
  - `/aexp-close-batch` → `/aexp-finding-from-batch` (batch selector)
  - `/aexp-new-finding` → `/aexp-finding-placeholder` (no citations yet;
    synthesis / deferred)

  Each file now routes through `aexp new-finding` (the CLI verb landed
  earlier in this Unreleased cycle) for id allocation + automatic
  parent-backlink patching, and documents the three-command set up-front
  so users pick by intent at a glance.
- **`docs/tracker-adapters.md`** reframed. The `TrackerAdapter` /
  `bind-tracker` / `WandbAdapter` path is no longer labeled "legacy" —
  it's the correct surface for CLI / subprocess / cluster workflows
  where the training code can't reach Python and needs the binding
  stamped from outside. Four modes now documented as equally legitimate
  (`tracked_run`, `prepare_tracker`, CLI `aexp bind-tracker`, noop /
  custom adapter); the choice is who controls the `wandb.init` call
  site, not which one is "new" or "deprecated".
- **`/aexp-new-run` next-step text** rewritten to point at all three
  paths (managed / BYO-init via Python, or CLI `bind-tracker` with
  `wandb.init(resume="allow", id=...)` in a separate training script)
  with the right use-case framing for each. Previously pointed only at
  `aexp bind-tracker`, which was mislabeled as the sole wandb path in
  earlier docs.

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
