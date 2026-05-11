# Sandbox scaffolding (`aexp.sandbox`)

A small surface for **agent-driven exploratory notebook work** that
isn't yet ready to live in the tracked H → E → F artifact graph. A
sandbox is reversible (`git checkout` undoes everything), bounded
(one subdirectory per attempt), and explicitly outside `kb_write_guard`
validation — so the agent can iterate freely without tripping the
artifact discipline that exists for paper-load-bearing work.

## When to use a sandbox

| You are…                                                            | Use…                       |
| ------------------------------------------------------------------- | -------------------------- |
| …iterating on a directional hunch; not sure it'll survive feasibility | sandbox                    |
| …confirming a calibrated prediction whose result will be cited     | `H### → E### → F###` chain |
| …prototyping helpers / shapes / glue you might keep                  | sandbox                    |
| …running the production-scale grid                                   | tracked experiment + queue |

The sandbox is **autonomous-write territory for the agent**: notebook
cells, kernel-written outputs, and `helpers.py` edits inside one
sandbox subdir don't require per-edit permission. Everything *outside*
the sandbox (package code, configs, `kb/` artifacts, canon) remains
git-dance or explicit-permission. This boundary is encoded in the
scaffolded root README, not enforced by aexp tooling.

## Layout

```
notebooks/
  _sandbox/                                      ← sandbox root
    README.md                                    ← autonomy-boundary docs
    .gitignore                                   ← excludes *.npy, *.parquet, large/
    2026-05-11_my-experiment/                    ← per-experiment subdir
      README.md                                  ← experiment-design template
      helpers.py                                 ← sandbox-local utilities
      00_feasibility.ipynb                       ← (user-created)
      01_calibration.ipynb                       ← (user-created)
      outputs/
        plot.png                                 ← tracked (small)
        large/big_array.npy                      ← gitignored
```

The per-experiment subdir name is `<YYYY-MM-DD>_<slug>`. The
sandbox root (`README.md` + `.gitignore`) is created on the *first*
`/aexp-new-sandbox` invocation and preserved on subsequent runs — a
hand-edited root README is never overwritten.

## Slash command

The canonical entry point is `/aexp-new-sandbox`. From inside Claude
Code:

```
/aexp-new-sandbox
```

The skill asks for a slug (filesystem-safe; lowercase, hyphen-separated,
alnum-only), optionally a human-readable title (defaults to a
Title-Cased version of the slug), and optionally a parent directory
override. It then calls the CLI verb described below and reports what it
created.

## CLI

```
aexp new-sandbox --slug "<slug>" \
    [--title "<human title>"] \
    [--parent-dir "<override>"]
```

Concrete example:

```bash
aexp new-sandbox --slug "prompt-brittleness" \
    --title "Prompt-brittleness Characterization"
```

Output (first invocation in a repo — initializes the sandbox root):

```
Created notebooks/_sandbox/2026-05-11_prompt-brittleness/
  + README.md
  + helpers.py
Initialized sandbox root:
  + notebooks/_sandbox/README.md
  + notebooks/_sandbox/.gitignore
```

Subsequent invocations skip the root-init step:

```
Created notebooks/_sandbox/2026-05-11_compositional-probe/
  + README.md
  + helpers.py
```

The CLI verb is idempotent at the directory-name level: re-running with
the same slug on the same date raises `SandboxScaffoldError` rather
than clobbering an existing experiment.

## Python API

For programmatic use (e.g. test setup, a custom slash command):

```python
from aexp.sandbox import scaffold

result = scaffold(
    slug="prompt-brittleness",
    title="Prompt-brittleness Characterization",
    # parent_dir=...,    optional override (default: notebooks/_sandbox)
    # repo_root=...,     optional explicit root (default: walk up from cwd)
    # today=...,         optional date_cls override (for deterministic tests)
)
print(result.dir_path)            # "notebooks/_sandbox/2026-05-11_prompt-brittleness"
print(result.files_created)       # ["...README.md", "...helpers.py", ...]
print(result.root_initialized)    # True on first call in a repo
```

`scaffold()` returns a frozen `SandboxScaffoldResult` dataclass with
`slug`, `dir_name`, `dir_path` (repo-relative POSIX), `files_created`,
and `root_initialized`. Raises `SandboxScaffoldError` on invalid slug
or pre-existing directory.

`slugify(title)` is also exported for callers that want to compute the
slug themselves; it matches `aexp.artifacts.slugify` so the same input
produces identical slugs across sandbox and tracked-artifact creation.

## The notebook first-cell convention

A sandbox notebook should start with:

```python
from aexp.sandbox import setup_sandbox_notebook
ctx = setup_sandbox_notebook("2026-05-11_prompt-brittleness")
import helpers
```

`setup_sandbox_notebook(name)` does three things:

1. Walks up from `Path.cwd()` to find the git repo root.
2. Locates `<repo>/notebooks/_sandbox/<name>/`.
3. Inserts that directory at the front of `sys.path` so
   `import helpers` resolves to *this* experiment's `helpers.py`,
   not some other `helpers` module shadowed by import-path order.

It returns `{"repo_root": Path, "sandbox_dir": Path}` so the notebook
can build absolute paths without re-doing the search.

**Why this matters.** On a remote Jupyter server the kernel's `cwd`
is the notebook's directory, *not* the repo root. Naive
`Path("notebooks/...").resolve()` from a notebook one sandbox dir deep
doubles the path (`<root>/notebooks/_sandbox/<x>/notebooks/_sandbox/<x>`)
and fails silently. `setup_sandbox_notebook` was the friction-design
fix for that gotcha (logged as F4 in the upstream electricrag session
that motivated this surface).

## What gets scaffolded

### Per-experiment `README.md`

A directional-experiment-design template with these sections:

- **Mode** (`exploratory` by default; sandbox is not paper-cite-able
  until promoted)
- **Origin context** (pointer to the session note / motivation)
- **Statement** (directional, no numerical thresholds — refine via
  calibration)
- **Mechanism** (architectural / theoretical reason it might hold)
- **Why this might generalize**
- **Shortcut risks** (≥3 with mitigations)
- **Test plan** (val/test split, conditions, infrastructure used)
- **Stages** (Stage 0 — feasibility; Stage 1+ as needed)
- **Open questions**
- **Cross-references**

The template is biased toward exploratory framing — no fabricated
confirm/reject thresholds, no presupposed numerical predictions. Fill
the placeholders as the experiment takes shape; this is the right place
to refine framing *before* promoting to a tracked H.

### Per-experiment `helpers.py`

A small skeleton with `SANDBOX_DIR` and `REPO_ROOT` pre-resolved via
`aexp.utils.paths.find_repo_root`, plus a usage docstring. Edit freely
— this file is owned by this experiment subdir. If a helper proves
reusable across experiments, that's the cue to promote it to a real
package module (with the user in the loop, since that's a cross-cutting
change).

### Sandbox root `README.md` (first invocation only)

Describes the autonomy boundary, the `<YYYY-MM-DD>_<slug>/` convention,
the promotion path through `/aexp-new-thread → /aexp-new-hypothesis →
/aexp-new-experiment → /aexp-promote-nb`, and the small-vs-large
outputs distinction. Hand-editable; never re-overwritten by subsequent
`/aexp-new-sandbox` calls.

### Sandbox root `.gitignore` (first invocation only)

Excludes large output formats from version control while keeping the
notebooks, helpers, and small artifacts tracked:

```
**/*.npy
**/*.parquet
**/*.h5
**/*.feather
**/outputs/large/**
**/.ipynb_checkpoints/
```

## Promotion path

When a sandbox experiment's result is going to be cited as a paper
finding, walk it into the tracked-artifact chain:

1. `/aexp-new-thread` — if no parent thread exists yet
2. `/aexp-new-hypothesis --thread T###` — directional predictions;
   exploratory framing is fine for post-hoc hypotheses derived from
   sandbox smoke results
3. `/aexp-new-experiment --hypothesis H###` — the formal experiment
4. `/aexp-promote-nb` — extracts working cells from the sandbox
   notebook into a tracked-run Python script

Until you reach step 4, just iterate in the sandbox. There's no penalty
for sandbox work that doesn't promote — that's the whole point of
having a free-form workspace.

## Provenance

The sandbox surface was crystallized during the 2026-05-10 electricrag
session that motivated the agentic-experiments AFK port. The original
scaffold lived in electricrag-local Python before being lifted into
`aexp.sandbox`. The notebook first-cell convention
(`setup_sandbox_notebook`) was designed to close the F4 friction
documented in `electricrag/docs/reference/process/environment.md` —
naive path resolution on remote Jupyter doubling the sandbox path.

The directional-statement template — no fabricated thresholds, ≥3
shortcut risks, explicit "Mode: exploratory" framing — mirrors the
dual-mode `## Intent` section added to the tracked-experiment template
in 0.2.0. Both exist to make THARKing-honest exploratory framing the
path of least resistance.
