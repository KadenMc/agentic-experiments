# Changelog

All notable changes to `agentic-experiments` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`aexp install --force` no longer clobbers user-authored scaffold
  content** in `kb/` and `templates/`. Previously `--force` treated every
  shipped file (tooling + scaffold) the same, so a user who had polished
  `kb/mission/CHALLENGE.md`, `kb/ACTIVE.md`, `kb/DASHBOARD.md`, or any
  `templates/*.md` and then re-ran install to refresh a slash command
  would silently lose that content back to the blank default. Reported
  by the electricrag-side agent on 2026-04-24 after a committed
  `CHALLENGE.md` was reverted to `{What should the agent research?}`.

  Fix: the scaffold trees (`_TREES_VERBATIM = ("kb", "templates")`) now
  opt into a content-diff preservation rule in `_copy_file`. If the
  target exists and its bytes differ from the shipped source, the
  installer emits a new `preserved_user_modified` action and leaves the
  file alone — even under `--force`. Files that byte-match the shipped
  default still refresh (trivially, via the existing `skipped_identical`
  path). Tooling files (slash commands, skills, hooks, `.mcp.json`,
  `.claude/settings.json`) continue to honor `--force` — their refresh
  is the whole point of the flag.

  Escape hatch: if you genuinely want to nuke a scaffold file back to
  the default, `rm` it before re-installing. `--force` alone is no
  longer sufficient for that destructive semantic.

  The install summary gets a new `preserved_user_modified` row so the
  behaviour is visible, and the CLI prints a per-file `preserved_user_modified
  <path>: kept your content; shipped default not applied` line for each
  such file. 5 new tests in `tests/test_install.py` cover: `kb/`
  preservation under both no-force and `--force`, `templates/`
  preservation under `--force`, `skipped_identical` when content
  matches, and the invariant that slash commands (tooling) still refresh
  under `--force`.

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

### Added (queue + runner materialization + sp resolution)

- **`aexp.queue` module** — organizational queue over signac. Agents
  register pending runs on one machine (laptop / MCP host) and materialize
  them as a runner script (shell / slurm / manual) that executes on
  another (e.g. an HPC cluster the agent can't directly access). Public
  API: `add_to_queue`, `add_many_to_queue`, `list_queue`,
  `remove_from_queue`, `clear_queue`, `materialize_queue`, `run_queued`,
  `resolve_sp`, `render_runner_command`, `parse_sweep`, `QueueEntry`,
  `MaterializeResult`. Errors: `RunnerCommandMissing`, `SubprocessFailed`,
  `SweepParseError`.
- **New `RunStatus` value `"queued"`** — extends the lifecycle to
  `created → queued → running → complete|failed|abandoned`. Backward-
  compatible: `create_run` still initializes `"created"`; only
  `add_to_queue` writes `"queued"`.
- **sp resolution (drift-proof provenance)** — named `conditions:`
  blocks in an experiment's frontmatter are merged into a job's state
  point *at queue-time*, so `--sp condition=full` resolves to the full
  config (e.g. `model`, `max_turns`, `tools`) and signac freezes it to
  `signac_statepoint.json`. Later edits to `conditions.full` can't
  retroactively change what queued-then-ran. User sp keys win on
  collision. Enabled by default on both `queue add` and `new-run`
  (`create_run` gained a `resolve_conditions=True` kwarg); opt out with
  `--no-resolve` (CLI) / `resolve_conditions=False` (Python).
- **`{sp_json}` runner-command placeholder** — renders the full resolved
  sp as compact JSON (no whitespace) so it splats safely into a shell
  argv. Plus `{key}` for any sp field and `{job_id}` for the 32-hex id.
- **CLI `queue` subcommand group** — `aexp queue add`, `queue list`,
  `queue remove`, `queue clear`, `queue materialize`. First use of
  `app.add_typer(...)` in the codebase. Plus top-level `aexp run-queued`
  for runner-side execution of one queued job.
- **`--sweep` grammar** — `aexp queue add --sweep "condition=full|cls,
  seed=0..3"` expands to a Cartesian product. `|` separates enumerated
  values; `a..b` is an inclusive integer range. Keys in `--sp` cannot
  overlap with `--sweep` keys.
- **MCP tools** — `queue_add`, `queue_list`, `queue_remove`,
  `queue_clear`, `queue_materialize`. `run_queued` is deliberately NOT
  exposed via MCP: execution on the MCP host is usually the wrong env.
- **Three new slash commands** — `/aexp-queue-add`, `/aexp-queue-list`,
  `/aexp-queue-materialize`. Ends at 14 total slash commands.
- **Validator extension** — `kb_validate` gets a `conditions_schema`
  check: when an experiment's `conditions:` field is present, each
  named block must be a dict of JSON-serializable primitives. Absent
  field = no-op (backward compatible).
- **Runner-env injection** — `aexp run-queued` sets `AEXP_JOB_ID` and
  `AEXP_JOB_WORKSPACE` in the subprocess environment so training scripts
  can find their own job without argv threading.
- **Failure forensics** — `run_queued` captures the last 2KB of stderr
  into `job.doc["queue"]["last_error"]` before `run_lifecycle` marks
  the job failed. `aexp queue list --include-terminal` surfaces this.
- **`docs/queue.md`** — canonical guide: three runner modes, sp
  resolution semantics, `runner_command` placeholders, cross-machine
  sync workflow (git-based), FAQ on failure modes.

### Changed (queue-related)

- **`create_run` gains `resolve_conditions=True` kwarg.** Default-on so
  `aexp new-run --sp condition=full` resolves the same way
  `aexp queue add` does. Pass `resolve_conditions=False` to store a
  bare label (the pre-queue behavior).
- **Experiment template** (`src/aexp/vendor/limina/templates/experiment.md`)
  — frontmatter gains commented-out `runner_command` and `conditions`
  stubs so new experiments know where to opt in. Existing experiments
  aren't rewritten; validator ignores absent / empty fields.
- **`docs/quickstart.md`** — adds a "5b. Batch-queue for cluster /
  batched execution" section showing the sweep → materialize → submit
  workflow with a sample `conditions:` block.
- **`docs/cli.md`** — lists the `queue` subcommand group and
  `run-queued` under "Verbs at a glance"; points at `docs/queue.md`
  for the model.
- **`README.md`** — adds the queue row to the capabilities table,
  bumps CLI / MCP / slash-command counts, drops queue from the v1.1
  backlog, adds `docs/queue.md` to the doc index.

### Added (queue-run + honest slurm template)

- **`aexp queue run`** — the inside-your-batch-script iteration primitive.
  Iterates the pending queue filtered by `--experiment` / `--tag` and
  executes each job via `run_queued` semantics. Two shapes: sequential
  (no flag) runs every matching job in stable order; `--index N` picks
  the Nth pending job for slurm-array deployment (`--index
  "$SLURM_ARRAY_TASK_ID"`). `--continue-on-failure` keeps iterating past
  errors; default is fail-fast with `SubprocessFailed`.
- **`aexp.run_queue(...)`** — Python API for the same iteration, exported
  at the package root. Returns a list of subprocess returncodes.

### Changed (honest slurm template)

- **`aexp queue materialize --runner slurm`** now emits a **starter
  template** with explicit `# TODO` placeholders for `#SBATCH`
  directives the user must fill in (partition, account, time, mem, gpus)
  and commented-out setup commands (module loads, env activation,
  working-dir `cd`). The template's aexp-specific line is
  `exec aexp queue run --tag <tag> --index "$SLURM_ARRAY_TASK_ID"` —
  job ids are resolved at task-launch time, not baked into a bash array
  at materialize-time, so re-queueing between submit and execute stays
  coherent.

  Rationale: aexp has zero visibility into cluster conventions, so
  emitting a "ready to submit" slurm script is a lie. The previous
  output pretended to be turn-key; the new template is explicit about
  the user's responsibility and encourages skipping the generated file
  entirely in favor of adding one `aexp queue run` line to whatever
  batch script the user already has working for their site.
- **`docs/queue.md`** reframed: `aexp queue run` is documented as the
  primary cluster primitive; `materialize --runner slurm` is demoted to
  a starter-template convenience.
- **`docs/cli.md`** / **`docs/quickstart.md`** updated with the
  in-script `queue run` pattern as the canonical cluster flow.
- **Tag semantics clarified in docs.** `--tag` is pure metadata — a
  user-chosen label aexp stores and filters by, with no scheduling /
  deadline / wall-clock semantics. All example tags in `docs/queue.md`,
  `docs/quickstart.md`, and the queue-adjacent slash commands are now
  named by *what the batch is* (`paper-ablation`, `full-vs-classify`,
  `seed-stability`) instead of temporal markers (`overnight`,
  `tonight`) that falsely implied aexp cared about timing. A new
  "What `--tag` is (and isn't)" section in `docs/queue.md` spells this
  out so agents reading the docs don't infer semantics we don't have.

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
