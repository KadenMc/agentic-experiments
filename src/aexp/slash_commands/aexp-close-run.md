---
description: "Draft a Finding (F###) citing a single signac run."
---

Close out a single run by drafting an `F###` Finding that cites it.

> **Invocation note.** Use `python -m aexp <verb>`. If your
> `python` doesn't have the package, prefix with
> `conda run -n <env>` (env name in `.aexp/installed.json`,
> field `conda_env_name`) or use the absolute `python_exe` from the same
> marker. Avoid the `aex` shim from slash commands — Claude Code's bash
> PATH may not resolve it.

> **Backlink rule.** `kb_write_guard` enforces bidirectional wiki-links.
> Creating F001 that links `[[H001]]` / `[[E001]]` requires the same
> Hypothesis + Experiment files to list `[[F001]]` in their `## Links`
> sections. This is a three-file change: write F001, edit H001, edit E001.
> If you skip the backlinks, the PostToolUse hook will flag the finding
> as invalid (the file still lands on disk, but every subsequent validate
> will fail until the backlinks are added).

Flow:

1. Ask the user for the `<job_id>` (short or full). Use
   `python -m aexp show-run <job_id>` to confirm the run
   exists and what experiment/hypothesis it's linked to.
2. Run the vendored artifact creator to mint a new Finding id:

   ```
   python scripts/kb_new_artifact.py finding
   ```

   When it prompts for `hypothesis` and `experiment`, fill them from the
   run's `doc["limina"]` (read them from
   `python -m aexp show-run`). Slug should be short and
   descriptive — often `<experiment>-verdict` is fine.
3. Open the newly created `kb/research/findings/F###-*.md` and:
   - Add `supporting_runs:` to the YAML frontmatter as a **list of
     mappings** — never bare strings. Single-run form:

     ```yaml
     supporting_runs:
       - type: job
         id: "<full-32-hex-job-id>"
     ```

   - Pre-fill the `## Results` / `## Analysis` sections from
     `job.doc["summary_metrics"]` if present.
   - Leave the `## Decision` section blank for the user to write.
   - Include a `## Links` section linking back to the hypothesis,
     experiment, `[[ACTIVE]]`, and `[[CHALLENGE]]`.
4. **Add backlinks** to the parent artifacts:
   - Edit `kb/research/hypotheses/H###-*.md` → add `- [[F###]]` to its
     `## Links` section.
   - Edit `kb/research/experiments/E###-*.md` → add `- [[F###]]` to its
     `## Links` section.
5. Run `python -m aexp validate` to confirm the Finding
   passes both the KB and run-citation checks.
