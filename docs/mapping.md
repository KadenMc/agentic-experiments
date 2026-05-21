# Artifact IDs ↔ signac jobs ↔ W&B runs

The single most important doc. If you read one file, read this one.

## The short version

```
Hypothesis (H###)           ── in kb/research/hypotheses/
     │
     ▼
Experiment (E###)           ── in kb/research/experiments/
     │  declares:  hypothesis: H###,  sub_hypotheses: [H###, ...]
     ▼
signac Job                  ── in .runs/workspace/<job_id>/
     │  sp:   experiment_id, hypothesis_id, condition, model, seed, code_commit, ...
     │  doc:  aexp={...}, status, tracker={...}, summary_metrics, ...
     ▼
W&B Run                     ── project=<user-supplied>
        group=<hypothesis_id>/<experiment_id>/<condition>
        config={**sp, aexp, frame, job_id}
     │
Finding (F###)              ── in kb/research/findings/
   supporting_runs: [{type: job, id: <job_id>} | {type: batch, experiment_id, selector}]
```

## State point — what goes in identity

`job.sp` is hashed to form the job's directory name. Change any value → new
directory → new run. These are the keys `create_run` knows about:

| Key | Source | Meaning |
|---|---|---|
| `experiment_id` | always auto-added | `E###` link (mirror of `doc["aexp"]`) |
| `hypothesis_id` | if passed to `create_run` | Primary or sub-hypothesis link |
| `sub_hypothesis_id` | if passed | Narrower framing within an experiment |
| `code_commit`, `code_dirty` | `git rev-parse HEAD` at creation, unless `include_commit=False` | Reproducibility pin |
| everything else | user-supplied `statepoint={}` dict | Whatever defines this run's identity |

User-supplied keys override the auto-defaults, so you can pin a specific
commit for replay by passing `code_commit="abc1234"` yourself.

## Job document — what's mutable

`job.doc` is where mutation happens. Lifecycle + tracker + metrics go here:

```python
{
  "aexp": {
    "experiment_id":     "E018",
    "experiment_path":   "kb/research/experiments/E018-paired-ablation.md",
    "hypothesis_id":     "H012",
    "sub_hypothesis_id": None,
  },
  "status":       "complete",                   # created | running | complete | failed | abandoned
  "created_at":   "2026-04-20T15:00:00Z",
  "started_at":   "2026-04-20T15:00:03Z",
  "ended_at":     "2026-04-20T15:14:27Z",
  "wallclock_s":  864.421,
  "tracker": {
    "backend":  "wandb",
    "run_id":   "abcdef12",
    "url":      "https://wandb.ai/...",
    "project":  "ecg-inquiry-eval",
    "group":    "H012/E018/full",
  },
  "summary_metrics": { "accuracy": 0.83, "n": 32 },
  "tags": ["smoke"],
}
```

The status lifecycle is owned by `run_lifecycle(job)` — a context manager
that flips `created -> running -> complete` (or `failed` on exception).

## Batch slug — one rule, used everywhere

```python
def batch_slug(*, hypothesis_id, experiment_id, condition, fallback):
    return f"{hypothesis_id or '_'}/{experiment_id}/{condition or fallback}"
```

This function drives:
- W&B group on every `bind_tracker` call.
- Batch identity in `aexp list-batches` / `aexp show-batch`.
- Tag set attached to tracker runs.

Example: `H012/E018/full`, or `_/E001/abcdef12` when no hypothesis + no
condition (fallback is the job's short id).

## Finding supporting_runs — citation shape

Every Finding must cite the runs that motivated its verdict. Two forms:

```yaml
# Single-run citation
supporting_runs:
  - type: job
    id: "abcdef01234567890abcdef01234567890abcdef"   # 32-hex signac job id

# Batch citation (any slice that matches jobs)
supporting_runs:
  - type: batch
    experiment_id: "E018"
    selector:
      condition: "full"
      # model: "gpt-oss-20b"   # optional additional filters
```

`aexp validate` checks:

- `finding.broken_run_citation` — job id doesn't exist under `.runs/workspace/`.
- `finding.empty_batch` — selector matches zero jobs.

## Validator error codes (cheat sheet)

| Code | Meaning | Fix |
|---|---|---|
| `aexp.validation_failed` | The bundled `kb_validate.py` reported errors | Read the details; usually missing `## Links`, missing frontmatter field, broken wikilink |
| `run.orphan` | A signac job has no `doc["aexp"]` | `aexp link <job_id> --experiment E###` |
| `run.broken_experiment_link` | Run references an E### with no file on disk | Fix the link, or create the experiment via `kb_new_artifact.py` |
| `run.hypothesis_mismatch` | Run's `hypothesis_id` isn't the experiment's primary or a sub | Fix the run or add the hypothesis to the experiment's `sub_hypotheses:` |
| `run.sub_hypothesis_unlisted` | Run claims a sub-hypothesis not in experiment's `sub_hypotheses:` | Same fix |
| `run.status_invalid` | Run has `status` outside `{created, running, complete, failed, abandoned}` | Set a valid status |
| `finding.broken_run_citation` | Finding cites a job id that doesn't exist | Fix the id or remove the citation |
| `finding.empty_batch` | Finding cites a batch with zero matching runs | Fix the selector |
