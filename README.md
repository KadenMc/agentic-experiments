# agentic-experiments

Git-first, hypothesis-forcing experiment tracking for agent-driven ML research.

**Three layers, each owned by a different component:**

| Layer | Owner | What lives here |
|---|---|---|
| Research harness | Vendored **[Limina](https://github.com/KadenMc/limina)** (forked into this package) | `kb/` artifact graph — Hypothesis → Experiment → Finding, plus Literature / Challenge Review / Strategic Review; templates; Claude Code hooks enforcing the H→E→F chain |
| Local execution / run state | **[signac](https://signac.readthedocs.io)** | `.runs/.signac/` + `.runs/workspace/<job_id>/` per run. `job.sp` = identity params; `job.doc` = Limina link, tracker IDs, status, summary metrics |
| Observability mirror | **W&B** (optional `[wandb]` extra) | Remote runs grouped by a deterministic slug derived from the Limina context |

See the design plan for details:
`C:/Users/Owner/.claude/plans/c-users-owner-claude-plans-dreamy-munch-rustling-babbage.md`.

Claude-quickstart hint:
`powershell.exe -Command "cd C:\Vaults\SecondBrain\repos\agentic-experiments; conda activate agentic-exp; aex --help"`

## Setup

Developed in and tested using Python 3.12. Matches the `electricrag` pattern: Poetry for deps, conda for env.

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

# 4. Install the package + dev deps + wandb extra
cd C:\Vaults\SecondBrain\repos\agentic-experiments
poetry install --with dev --extras wandb

# 5. Sanity check
aex version
# expected: 0.1.0

aex --help
# expected: lists install, new-run, list-runs, list-batches, show-run,
#           show-batch, link, bind-tracker, validate, install-slash-commands

python -c "import signac, frontmatter, pydantic, typer; print('ok')"
```

## Dependency groups

| Group | What / when |
|---|---|
| `main` (default) | Runtime deps: `signac`, `python-frontmatter`, `pydantic`, `pyyaml`, `typer`, `rich`. |
| `--with dev` | `pytest`, `pytest-cov`, `ruff`, `mypy`. |
| `--extras wandb` | `wandb` — for the W&B tracker adapter. |

Install subsets as needed, e.g. `poetry install` (main only), `poetry install --with dev` (main + dev, no wandb).

## Layout

```
src/agentic_experiments/
  __init__.py           # public API re-exports
  cli.py                # Typer app (aex)
  __main__.py           # python -m agentic_experiments → CLI
  # forthcoming:
  install.py            # install_limina (apply vendored Limina into a consumer repo)
  runs.py               # signac wrappers: create_run, open_run, find_runs, mark_running
  linking.py            # Limina ↔ signac link helpers
  limina_io.py          # typed read wrappers for H/E/F/L/CR/SR
  validate.py           # composes kb_validate + run-link integrity
  schema.py             # pydantic models: RunLink, RunSummary, LiminaArtifactRef, Issue
  vendor/limina/        # forked Limina snapshot (hooks ported to Python)
  trackers/             # TrackerAdapter ABC + wandb_adapter + noop_adapter
  utils/                # paths, git, atomic writes
reference/limina/       # original Limina 0.1.0 snapshot, committed once for diff reference
tests/
docs/
```

## Status

Very early. Scaffold + CLI surface only; the real implementation lands next (see
plan §11 for the work order). `aex <verb>` currently returns `not implemented yet`
for every verb except `version` and `--help`.

## License

MIT. See `LICENSE`.
