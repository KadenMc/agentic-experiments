# Concepts

`agentic-experiments` is a **fusion layer**, not a new framework. It builds
on [Limina](https://github.com/KadenMc/limina) for the research-graph
primitives (H→E→F artifact model, templates, methodology skills), uses
[signac](https://signac.readthedocs.io) for local execution and run state,
and bridges to W&B for optional remote observability.

## Three layers

| Layer | What lives here | Artifacts |
|---|---|---|
| **Research grammar** | `kb/` artifact graph — Hypothesis → Experiment → Finding, plus Literature / Challenge Review / Strategic Review; artifact templates; Claude Code hooks enforcing the H→E→F chain | `kb/research/hypotheses/H###-*.md`, `kb/research/experiments/E###-*.md`, `kb/research/findings/F###-*.md` |
| **Local run state** (signac) | `.runs/.signac/` + one workspace dir per run. Identity via **state point**, mutable metadata in **job document** | `.runs/workspace/<job_id>/` |
| **Observability** (W&B, optional) | Remote runs grouped deterministically from the `(hypothesis, experiment, condition)` slug | W&B project + group |

## The hermeneutic loop this enables

For any claim a user holds, they can trace:

- **down** to the runs that produced it: a `Finding` cites `supporting_runs:` → each run's `.runs/workspace/<id>/` preserves outputs + `job.doc["limina"]` to navigate back.
- **up** to the question it was meant to answer: from a run, `job.doc["limina"]["experiment_id"]` → `kb/research/experiments/E###-*.md` (frame + protocol) → `Hypothesis: H###` → `kb/research/hypotheses/H###-*.md`.

The bidirectional traversal *is* the coupling. If it breaks anywhere, the whole collapses into "some logs and some notes."

## Limina ↔ signac mapping

- One `E###` artifact = one research-level experiment (intent, protocol, success criteria). Human/agent-facing.
- One signac job = one concrete execution instance. Many jobs per `E###`.
- **`code.commit` goes in the state point**, so re-running at a new commit creates a new directory; everything persists. Configurable via `include_commit=False` on `create_run`.

### State point vs job document

- `job.sp` — identity-defining: `experiment_id`, `hypothesis_id` (optional), `condition`, `model`, `dataset_slice`, `seed`, `prompt_rev`, `code_commit`, and any consumer-specific params.
- `job.doc` — mutable: `limina` link dict, `status`, `started_at` / `ended_at` / `wallclock_s`, `tracker` (backend + run_id + url), `summary_metrics`, `tags`.

### Sub-hypotheses

A single `E###` can test multiple related hypotheses. Its frontmatter supports:

```yaml
hypothesis: "H012"                # primary
sub_hypotheses: ["H013", "H014"]  # optional, tested within this experiment
```

Runs may link to `H012`, `H013`, or `H014` via `sp.hypothesis_id` or `job.doc["limina"]["sub_hypothesis_id"]`. `aexp validate` checks that any claimed sub-hypothesis is in the experiment's listed `Sub-hypotheses`.

### Batch as a query-level concept

A *batch* is NOT a Limina artifact. It's a slice over `.runs/` defined by shared state-point values — most commonly `(experiment_id, condition)` — mapping 1:1 to a W&B group string. Use `aexp list-batches` / `aexp show-batch` to browse them. `batch_slug(hypothesis_id, experiment_id, condition, fallback)` is the single function that derives this slug everywhere (CLI tables, W&B group, closing findings).

## Linking direction of truth

- **Job → Limina**: `job.doc["limina"] = {"experiment_id": "E018", "hypothesis_id": "H012", "sub_hypothesis_id": null, "experiment_path": "kb/.../E018-*.md"}`.
- **Finding → Runs**: finding frontmatter field `supporting_runs:` — a list of `{type: job, id: ...}` OR `{type: batch, experiment_id, selector: {...}}` entries. Validated by `aexp validate`.
- **Job → Tracker**: `job.doc["tracker"] = {"backend": "wandb", "run_id": "...", "url": "...", "project": "...", "group": "..."}` — written by `bind_tracker`.

## What lives where

```
consumer-repo/
  kb/                            # scaffolded by `aexp install`
    ACTIVE.md, DASHBOARD.md
    mission/CHALLENGE.md
    research/{hypotheses,experiments,findings,literature,data}/
    reports/                     # CR + SR
    lessons/
  templates/                     # H/E/F/L/CR/SR/report artifact templates
  .claude/
    settings.json                # hooks -> "<python_exe>" -m aexp.hooks.<name>
    skills/                      # 4 research-methodology skills
  .mcp.json                      # `aexp` MCP server, uvx-invoked
  .runs/                         # signac project (configurable at install time)
    .signac/
    workspace/<job_id>/
  .aexp/
    installed.json               # version + run_store_path + python_exe + vendor sha
```

Hook scripts and validator code live inside the installed `aexp`
package — they never land in the consumer repo. Upgrades happen via
`pip install -U agentic-experiments`.

## Two validators, two scopes

There are two pieces of validation machinery, and they check different things:

| Validator | Runs when | Scope | Exit code surfaces |
|---|---|---|---|
| `aexp.kb_validate.validate_kb()` | `PostToolUse` on every kb-write (via `aexp.hooks.kb_write_guard`) and `Stop` at turn end (via `aexp.hooks.stop_validate`) | **KB structural only** — frontmatter required fields, filename format, ID aliases, wikilinks resolve, bidirectional backlinks (H↔E↔F), required H2 sections. | Claude Code hook (blocks turn / write) |
| `aexp.validate.validate_repo()` / `aexp validate` | Manually by the user or agent | **Everything above** (calls `validate_kb()` in-process) **plus** run-link integrity (`doc["limina"]`), `supporting_runs` citation checks, hypothesis-consistency between run and experiment. | CLI exit code 1 |

**Practical implication:** a Claude Code session can end cleanly (Stop hook
passes) while still containing broken `supporting_runs` citations. The
Stop hook does not catch them. Run `python -m aexp validate`
explicitly before considering a session "complete."

## Why fork, not depend, on Limina

Limina upstream ships a template-clone flow (`clone + rm .git + re-init`) that doesn't compose with *applying* a harness to an existing repo. So `aexp` forks the pieces it needs:

- Hook behavior has been ported into `aexp.hooks.*` and is invoked as Python modules from the installed package.
- The KB structural validator lives at `aexp.kb_validate` — in-process, no subprocess dance.
- `src/aexp/vendor/` retains the research-graph data assets that *do* belong in every consumer repo: the `kb/` scaffold, artifact `templates/`, and the four methodology skills (`experiment-rigor`, `exploratory-sota-research`, `research-devil-advocate`, `build-maintainable-software`). These are the parts the agent actually reads and writes; keeping them checked into `aexp` lets `aexp install` drop them in verbatim, with merge policies that preserve user customizations.

One-time fork — no resync.

## Why no Weave / OpenTelemetry in v1

The runtime is **Claude Code / Claude Desktop**, not an SDK-driven agent loop. Our Python never sees `anthropic.messages.create()`. Weave's value (prompt/completion auto-instrumentation) collapses; what's left is a generic function tracer not worth the W&B-account + SDK weight. A future `[otel]` extra is a plausible v1.1 addition — Claude Code has OTEL emission built in (`CLAUDE_CODE_ENABLE_TELEMETRY=1`), so our spans could land in the same collector and correlate by session id. Deferred.
