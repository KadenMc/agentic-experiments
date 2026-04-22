# agentic-experiments

Git-first, hypothesis-forcing experiment tracking for agent-driven ML research.

- **PyPI distribution name:** `agentic-experiments` → `pip install agentic-experiments`
- **Python import name:** `aexp` → `from aexp import ...`
- **CLI entry point:** `aexp <verb>`
- **Slash-command prefix:** `/aexp-<verb>`
- **Claude Code MCP server name:** `aexp`

**Three layers, each owned by a different component:**

| Layer | Owner | What lives here |
|---|---|---|
| Research harness | Vendored **[Limina](https://github.com/KadenMc/limina)** (forked into this package) | `kb/` artifact graph — Hypothesis → Experiment → Finding, plus Literature / Challenge Review / Strategic Review; templates; Claude Code hooks enforcing the H→E→F chain; Limina research skills copied into `.claude/skills/` |
| Local execution / run state | **[signac](https://signac.readthedocs.io)** | `.runs/.signac/` + `.runs/workspace/<job_id>/` per run. `job.sp` = identity params; `job.doc` = Limina link, tracker IDs, status, summary metrics |
| Observability mirror | **W&B** (optional `[wandb]` extra) | Remote runs grouped by a deterministic slug derived from the Limina context |

Design doc: `C:/Users/Owner/.claude/plans/c-users-owner-claude-plans-dreamy-munch-rustling-babbage.md`.

Claude-quickstart hint:
`powershell.exe -Command "cd C:\Vaults\SecondBrain\repos\agentic-experiments; conda activate agentic-exp; aexp --help"`

## Onboarding — "clone and go"

**Prerequisite (one-time, per machine):** install [`uv`](https://docs.astral.sh/uv/) — Python's fast package/tool runner:

```powershell
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

That's it for global prerequisites — no conda, no manual Python install required.

**To use a repo that has `agentic-experiments` installed** (has a committed `.mcp.json` + installed Limina harness): just clone and open Claude Code.

```bash
git clone <repo-url>
cd <repo>
claude
# /mcp inside the session shows `aexp · ✓ connected`
# tools like new_run, list_runs, validate are available
```

`uvx` fetches `agentic-experiments` from PyPI on first use, caches it locally, and runs the MCP server in an isolated env. No project Python env required for MCP.

## Setup (for maintainers + researchers running experiments)

Developed in and tested using Python 3.12. Matches the `electricrag` pattern: Poetry for deps, conda for env (for running *experiments* — MCP server uses uvx independently).

```powershell
# One-time: make sure pipx + poetry are installed globally
python -m pip install --user pipx
python -m pipx ensurepath
pipx install poetry
```

Then:

```powershell
# 1. Create conda env (Python 3.12)
conda create -n agentic-exp python=3.12 -y

# 2. Activate
conda activate agentic-exp

# 3. Upgrade pip
python -m pip install --upgrade pip

# 4. Install the package + dev deps + all optional extras
cd C:\Vaults\SecondBrain\repos\agentic-experiments
poetry install --with dev --extras "wandb mcp"

# 5. Sanity check
aexp version
# expected: 0.1.0

aexp --help
# expected: lists install, new-run, list-runs, list-batches, show-run,
#           show-batch, link, bind-tracker, sync-offline, validate,
#           install-slash-commands

python -c "import aexp, signac, frontmatter, pydantic, typer, mcp; print('ok')"
```

## Invoking the CLI

Three equivalent entry points, listed in order of robustness under agent runtimes:

| Form | Best when |
|---|---|
| `conda run -n <env> python -m aexp <verb>` | **Most robust from inside Claude Code.** Works on Windows / macOS / Linux with no shell activation required. Only needs `conda` on PATH (which Claude Code's Bash tool inherits). ~200-400ms startup overhead per invocation. |
| `python -m aexp <verb>` | Works when `python` resolves to the env with the package — e.g. a PowerShell / zsh / bash where you've already `conda activate`d, or any venv with `pip install -e .`. |
| `aexp <verb>` | Shortest; only on PATH in human terminals with the env active. Claude Code's Bash tool (Git Bash on Windows, plain bash elsewhere) does not reliably resolve the Poetry-installed shim. |

**`.aexp/installed.json`** records the Python interpreter path (`python_exe`) and the conda env name (`conda_env_name`, empty for venv installs) captured at install time. Slash commands + the MCP server reference these fields so you never need to guess the env name.

**Why Claude Code's Bash is fiddly.** `conda activate` modifies the PATH of the calling shell only; child processes don't always inherit those modifications (Git Bash on Windows keeps its own POSIX-style PATH). The `conda run` form sidesteps the whole issue.

## How Claude actually interacts with the harness

The package exposes three surfaces Claude Code can use, each with a different trigger and purpose:

| Surface | Where it lives | Triggered by | Purpose |
|---|---|---|---|
| **Hooks** | `.claude/settings.json` | Claude Code (automatic) — SessionStart, PreToolUse, PostToolUse, Stop | Deterministic guardrails — inject `kb/ACTIVE.md` at session start, block HEF-chain violations, validate KB writes, run KB validation at turn end |
| **Slash commands** | `.claude/commands/aexp-*.md` | User typing `/aexp-new-run` etc. | Guided multi-step workflows — create runs, draft findings, close batches. Internally call the MCP server. |
| **MCP tools** | `.claude/settings.json` `mcpServers.aexp` entry | Claude during a turn | Structured tool calls with typed JSON returns. Every CLI verb is also an MCP tool. |
| **Skills** | `.claude/skills/<name>/SKILL.md` | Agent via `$experiment-rigor` etc. (referenced in `AGENTS.md`) | Prompt-level methodology guides (experiment rigor, SOTA research, devil's advocate, maintainable software) |

The **CLI** is the human-facing surface — same code, but you call it from a terminal.

## Stop-hook scope caveat

When a Claude Code session ends, `stop_validate.py` runs the vendored
`scripts/kb_validate.py` — a **KB-structural** check (frontmatter, aliases,
wikilinks, bidirectional backlinks, H→E→F chain). It does **not** run
`aexp`'s run-link / finding-citation validator.

So a session can end cleanly with a broken `supporting_runs` citation
still present. Run `python -m aexp validate` explicitly for full-coverage
validation; treat Stop hook success as "KB structurally sound" not
"everything coherent."

## Dependency groups + extras

| Group | Installs | When to use |
|---|---|---|
| `main` (default) | `signac`, `python-frontmatter`, `pydantic`, `pyyaml`, `typer`, `rich` | Always |
| `--with dev` | `pytest`, `pytest-cov`, `ruff`, `mypy` | Developing the package |
| `--extras wandb` | `wandb` | W&B tracker adapter |
| `--extras mcp` | `mcp` | MCP server for Claude Code |

Install subsets: `poetry install` (main only); `poetry install --with dev --extras "wandb mcp"` (everything).

## Layout

```
src/aexp/
  __init__.py           # public API re-exports
  cli.py                # Typer app (aexp)
  __main__.py           # python -m aexp → CLI
  install.py            # install_limina (apply vendored Limina into a consumer repo)
  runs.py               # signac wrappers: create_run, open_run, find_runs, run_lifecycle
  linking.py            # batch queries + retroactive Limina-signac linking
  limina_io.py          # typed read wrappers for H/E/F/L/CR/SR artifacts
  validate.py           # composes kb_validate + run-link + citation integrity
  schema.py             # pydantic + dataclass types (RunLink, BatchSummary, Issue, ...)
  mcp_server.py         # MCP server (FastMCP) — optional [mcp] extra
  slash_commands/       # /aexp-new-run, /aexp-close-run, /aexp-close-batch
  vendor/limina/        # forked Limina snapshot (hooks Python-ported + skills included)
  trackers/             # TrackerAdapter ABC + wandb_adapter + noop_adapter
  utils/                # paths, git, atomic writes
reference/limina/       # pristine upstream 0.1.0 snapshot, committed once for diff reference
tests/
docs/
```

## Status

v1 complete. Full pytest suite green on Python 3.12.13 / Windows. All CLI
verbs wired: `install`, `new-run`, `list-runs`, `list-batches`, `show-run`,
`show-batch`, `link`, `bind-tracker`, `sync-offline`, `validate`,
`install-slash-commands`. All MCP tools wired: `new_run`, `list_runs`,
`list_batches`, `show_run`, `show_batch`, `link_run`, `bind_tracker`,
`validate`, `sync_offline`.

HPC-friendly: W&B offline runs co-locate with their signac workspace
(`<repo>/.runs/workspace/<job_id>/wandb/offline-run-*/`), and
`aexp sync-offline` walks the run store and syncs every offline run in one
command from a login node.

Full end-to-end smoke (`tests/test_e2e_smoke.py`): fresh repo →
`aexp install` → create H+E → `aexp new-run` → `aexp bind-tracker --backend
noop` → `aexp list-batches` → `aexp validate` clean → break link →
`aexp validate` flags `run.broken_experiment_link` → re-run at new commit
produces a distinct persistent workspace.

Reserved for v1.1: artifact-creation CLI verbs (`aexp new-hypothesis` /
`new-experiment` / `new-finding`), `aexp index` dashboard, MLflow / Aim /
DVC tracker adapters, OpenTelemetry extra. No Weave in v1 — not a good fit
for the Claude Code / Desktop runtime (see `docs/tracker-adapters.md`).

See `docs/quickstart.md` for a worked example, `docs/mapping.md` for the
Limina↔signac↔W&B mapping details, `docs/cli.md` for the full CLI
reference, and `docs/mcp.md` for the MCP server + agent-side verification
prompt.

## License

MIT. See `LICENSE`.
