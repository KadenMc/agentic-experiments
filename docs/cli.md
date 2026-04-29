# CLI (`aexp`)

All verbs below work from any directory inside a repo that has
`.aexp/installed.json` (written by `aexp install`). They
resolve the repo root + run-store path by walking up for a `.git` dir or
the install marker, then reading the marker.

## Invocation — three entry points

Listed from most to least robust when running under Claude Code / inside
agent tools:

1. **`conda run -n <env> python -m aexp <verb>`**
   The robust default. Works on Windows / macOS / Linux from any shell
   with `conda` on PATH — no activation required. Adds ~200-400ms startup
   overhead per invocation. The env name is captured at install time in
   `.aexp/installed.json` → `conda_env_name`.

2. **`python -m aexp <verb>`**
   Works when `python` already resolves to the env that has the package.
   Use from an activated shell, or from a venv. Hard-coded in slash
   commands as the "primary" form with a preamble that explains how to
   fall back to (1).

3. **`aexp <verb>`**
   Shortest, but not always on PATH under Claude Code's Bash tool (Git
   Bash on Windows doesn't resolve Poetry's `.cmd` launcher via PATHEXT).
   Reserve for human PowerShell / zsh / bash sessions where you've
   activated the env.

If `python` in your context doesn't have the package (you'll see
`No module named aexp`), switch to form (1) using the
`conda_env_name` recorded in the install marker. Venv users substitute
the absolute `python_exe` from the same marker.

`aexp.utils.resolve_invocation(repo_root)` returns the
argv prefix to use for a given repo, reading the marker. Useful if a
script is composing command lines.

## Verbs at a glance

```
aexp install [--run-store PATH] [--force] [--no-require-git]
aexp version

aexp new-thread    --title "..." [--id T###] [--link <wikilink>...]
aexp list-threads  [--status STATE] [--tag TAG]
aexp show-thread   <T###>
aexp close-thread  <T###> [--conclusion "<markdown>"] [--promoted]

aexp new-run --experiment E### [--hypothesis H###] [--sub-hypothesis H###]
            [--sp K=V,...] [--no-commit]
aexp list-runs [--experiment E###] [--hypothesis H###] [--status STATUS]
              [--sp K=V,...]
aexp show-run <job_id>
aexp link <job_id> --experiment E### [--hypothesis H###] [--sub-hypothesis H###]

aexp list-batches [--experiment E###]
aexp show-batch   --experiment E### [--condition COND] [--model M]

aexp bind-tracker <job_id> --backend {noop,wandb} [--project P] [--offline]
aexp sync-offline [--dry-run]
aexp validate [--kb-only | --runs-only]
aexp install-slash-commands [--target .claude/commands]

# Queue subcommand group — pending-run registration + in-script execution + materialization
aexp queue add         --experiment E### [--sp K=V,...] [--sweep "K=V|V,K=a..b"]
                        [--tag T] [--hypothesis H###] [--no-resolve] [--no-commit]
                        [--allow-dup-on-recommit]
aexp queue list        [--experiment E###] [--tag T] [--include-terminal]
aexp queue run         [--experiment E###] [--tag T] [--index N]
                        [--continue-on-failure] [--force] [--dry-run]
aexp queue remove      <job_id>
aexp queue stop        <job_id> [--grace-s 5] [--force]
aexp queue clear       [--experiment E###] [--tag T] [--yes]
aexp queue materialize [--runner shell|slurm|manual] [--output PATH]
                        [--tag T] [--experiment E###]
                        [--slurm-time T] [--slurm-mem M] [--slurm-gpus N]
                        [--slurm-partition P] [--slurm-account A] [--slurm-extra "..."]
aexp run-queued <job_id> [--force] [--dry-run]
```

`aexp queue run` is the primary cluster primitive — call it from inside
your own batch script, sequential or array-parallel. `materialize` is
for users who want a starter template to customize; see
[`docs/queue.md`](queue.md) for why materializing a turn-key slurm
script is a lie aexp declined to tell.

See [docs/queue.md](queue.md) for the full queue model (sp resolution,
`runner_command` templates, cross-machine sync) — this file only lists
the CLI surface.

## Verbs — details

### `aexp install`

Apply the harness to the current repo: copy the `kb/` scaffold + `templates/`
(skipping any files the user has already changed), merge `.claude/settings.json`
with hook commands pinned to the current Python interpreter, write
`.mcp.json` (or JSON-merge our entry into an existing one), install the
four research skills into `.claude/skills/`, block-merge `AGENTS.md` and
`CLAUDE.md` under `<!-- agentic-experiments:begin -->` markers, initialise
`.runs/` as a signac project, and write the install marker.

By default the command prints a heads-up listing every file it will touch
and its merge policy, then prompts for confirmation before writing. Flags:

- `--dry-run` / `-n` — print the planned actions without writing anything.
- `--yes` / `-y` — skip the confirmation prompt (use in scripted / CI runs).
- `--force` — overwrite conflicting **tooling** files (slash commands, skills, hooks, `.mcp.json`). **User-authored scaffold content** under `kb/` and `templates/` is preserved even under `--force` (reported as `preserved_user_modified` in the install summary); delete the file first if you genuinely want to reset it to the shipped default.
- `--dev` / `-D` — write `.mcp.json` using the current Python interpreter
  (`"<python_exe>" -m aexp.mcp_server`) instead of the portable `uvx`/PyPI form.
  Use when you've installed `aexp` editable (`pip install -e`) and want edits
  to `src/aexp/mcp_server.py` to reach the MCP surface. The resulting
  `.mcp.json` bakes in a machine-specific path — gitignore it while iterating.
- `--run-store PATH` — override the default `.runs/` location (recorded in the marker).
- `--no-require-git` — install into a plain directory (useful for integration tests).

Re-runs are idempotent via a SHA-256 hash of the asset tree — if the install
marker matches the current sha, the command short-circuits with an
"already installed" note.

### `aexp new-run`

Create (or re-open) a signac job linked to a Limina experiment. Always
writes `job.doc["limina"]` and `job.doc["status"] = "created"`. `--sp` takes
`KEY=VAL,KEY=VAL` — all values stay as strings; use the Python API when you
need typed values (bools, ints, lists).

### `aexp list-runs`

Filter by experiment, hypothesis, status, or arbitrary sp keys. Table output
includes short id, experiment, hypothesis, status, condition, and tracker URL
if bound.

### `aexp show-run`

Print the full state point + doc + linked Limina frame for one run.

### `aexp list-batches` / `aexp show-batch`

`list-batches` groups runs by `(experiment_id, condition)` (default slice)
and rolls up counts + status mix + tracker group url. `show-batch` takes
explicit filters and returns the runs that match — useful before drafting a
batch Finding.

Change the grouping via the Python API: `list_batches(selector_keys=("condition", "model"))`.

### `aexp link`

Retroactively stamp `doc["limina"]` onto an existing job. Used when a job
was created outside `create_run` (e.g. from a notebook directly calling
signac) and you want to link it to an experiment after the fact.

### `aexp bind-tracker`

Start a tracker run and wire it to the job: group = `hypothesis/experiment/condition`,
tags auto-derived, config includes the full Limina chain + `job.sp` + a
curated frame (hypothesis statement, local hypothesis, success criteria).
`job.doc["tracker"]` stores the handle.

Backends: `noop` (writes JSONL into `<job_workspace>/tracker_log/`), `wandb`
(requires the `[wandb]` extra + `--project`; pass `--offline` on HPC nodes).

W&B runs always co-locate local state with the signac workspace. Offline-run
dirs land at `<job_workspace>/wandb/offline-run-*/`. Sync them later with
`aexp sync-offline` from a login node.

### `aexp sync-offline`

For HPC compute nodes without internet. Walks `.runs/workspace/*/wandb/`,
calls `wandb sync` on every `offline-run-*` found. Pass `--dry-run` to
preview what would be synced. Exits 1 if any single sync fails; 0 otherwise.

### `aexp validate`

Composes:

1. `aexp.kb_validate` (KB structural: frontmatter, wikilinks, backlinks, required sections) — called in-process.
2. Run-link integrity: `run.orphan`, `run.broken_experiment_link`, `run.hypothesis_mismatch`, `run.sub_hypothesis_unlisted`, `run.status_invalid`.
3. Finding citations: `finding.broken_run_citation`, `finding.empty_batch`.

Exit code 1 on any error. `--kb-only` skips runs-side; `--runs-only` skips the KB structural pass.

### `aexp install-slash-commands`

Copies the shipped slash commands (artifact creation: `aexp-new-hypothesis`,
`aexp-new-experiment`, `aexp-new-run`; finding creation:
`aexp-finding-from-run`, `aexp-finding-from-batch`,
`aexp-finding-placeholder`; read / inspect: `aexp-show-run`,
`aexp-show-batch`, `aexp-list-runs`, `aexp-status`, `aexp-validate`) into
`<target>/`, default `.claude/commands/`. Safe to re-run.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Validation errors (`aexp validate`) |
| 2 | User error (bad arg, missing project for wandb, etc.) |
