---
description: "Scaffold a new sandbox experiment subdirectory under `notebooks/_sandbox/`."
---

Create a new sandbox experiment directory for exploratory notebook work. `aexp` handles directory creation, README + helpers.py template rendering, and (on first use) the sandbox-root README + `.gitignore` for large outputs.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands.

## What this is

A **sandbox** is exploratory free-form work that hasn't yet promoted to a tracked Limina artifact. It's *not* in the H/E/F chain — no `kb_write_guard` validation applies, no artifact id is allocated. It's a working surface where iteration is cheap and reversible (`git checkout <slug-dir>` undoes everything).

Promote a sandbox experiment to the tracked-artifact graph when its result is going to be cited as a paper finding:

1. `/aexp-new-thread` (if no parent thread exists)
2. `/aexp-new-hypothesis --thread T###` — directional predictions; exploratory framing acceptable for post-hoc-from-smoke hypotheses
3. `/aexp-new-experiment --hypothesis H###`
4. `/aexp-promote-nb` extracts working cells into a tracked-run script

Until promotion, just iterate in the sandbox.

## Flow

1. **Ask the user for the experiment slug** — filesystem-safe (lowercase, hyphen-separated, alnum-only). Will become part of the directory name as `<YYYY-MM-DD>_<slug>/`. Keep slugs short and descriptive (e.g., `prompt-brittleness`, `compositional-probe`).

2. **Optional: ask for a human-readable title** for the per-experiment README's H1. Defaults to a Title-Cased version of the slug. Useful if the slug is terse and you want the README to be more descriptive.

3. **Optional: confirm the parent directory** if the user wants a non-default location. The default is `<repo>/notebooks/_sandbox/`. Override via `--parent-dir <path>` (relative paths resolve under repo root; absolute paths used as-is).

4. **Run:**

   ```
   python -m aexp new-sandbox --slug "<slug>" \
       [--title "<human title>"] \
       [--parent-dir "<override>"]
   ```

   The command creates `<parent_dir>/<YYYY-MM-DD>_<slug>/` populated with:
   - `README.md` — experiment-design template (Statement / Mechanism / Why-Generalize / Shortcut Risks / Test Plan / Stages / Open Questions / Cross-references). Fill these as the experiment takes shape; sandbox is the right place to refine framing before promoting to a tracked H.
   - `helpers.py` — sandbox-local utilities stub with the `SANDBOX_DIR` + `REPO_ROOT` boilerplate already wired up via `aexp.utils.paths.find_repo_root`.

   On first sandbox creation, also initializes the sandbox root (`notebooks/_sandbox/README.md` describing the autonomy boundary + the `.gitignore` excluding large outputs). Existing files are preserved — re-running won't overwrite a hand-edited root README.

5. **Show the user the result.** The command prints the created directory path and lists the files it wrote. If the sandbox root was initialized this call, that's noted too.

6. **Next step — first cell of the notebook.** The agent should remind the user of the canonical first-cell boilerplate for any notebook created inside the new sandbox subdir:

   ```python
   from aexp.sandbox import setup_sandbox_notebook
   ctx = setup_sandbox_notebook("<YYYY-MM-DD>_<slug>")
   import helpers
   ```

   This handles the kernel-cwd-vs-repo-root trap (the F4 friction) by walking up from `Path.cwd()` to find the git repo root, then locating the sandbox subdir robustly. Without it, naive `Path("notebooks/...").resolve()` doubles the path on remote Jupyter servers where the kernel's cwd is the notebook's directory.

## Conventions worth pointing out to the user

- **Multiple notebooks per subdir are fine** — name them `00_feasibility.ipynb`, `01_calibration.ipynb`, topical names, etc.
- **`helpers.py` is owned by this experiment** — edit freely. If a helper proves reusable across experiments, *that's* when to promote it to a real package module (via git-dance with the user in the loop).
- **Small outputs (plots, CSVs, JSONs) are tracked.** Large outputs (`*.npy`, `*.parquet`, `*.h5`, anything under `outputs/large/`) are gitignored via the sibling `.gitignore`.
- **The sandbox is autonomous-write territory for the agent.** Cells, kernel-written outputs, helper edits in this subdir don't require per-edit user permission. Everything *outside* the sandbox (package code, configs, `kb/` artifacts) remains git-dance or explicit-permission.
