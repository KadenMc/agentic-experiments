# Changelog

All notable changes to `agentic-experiments` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.2.1] — 2026-04-27

### Release summary

0.2.1 is a queue-layer bugfix release driven by early-adopter findings
from the electricrag F.1 inference session of 2026-04-26 → 2026-04-27.
The 0.2.0 queue layer worked end-to-end but had three discrete design
gaps that surfaced under real interactive cluster use; this release
closes all three plus three smaller pieces of side friction the same
session uncovered.

### Fixed (gap 1) — `aexp run-queued` streams subprocess output live

The 0.2.0 implementation invoked the runner via
`subprocess.run(..., capture_output=True)`, which buffers stdout and
stderr in memory until process exit then dumps them at once. For
interactive consumers (notebook runners, terminal `aexp run-queued`
calls) a 15-25 minute training run appeared totally silent until the
end. During the electricrag F.1 session this caused multiple
panic-kills of healthy jobs because the user couldn't tell whether
the work was alive vs hung.

`run_queued` now uses `subprocess.Popen` with line-by-line streaming
(`bufsize=1`, stderr merged into stdout for interleave-correct
ordering), and writes each line to the parent's stdout immediately
with a flush. A bounded `deque(maxlen=200)` ring buffer captures the
last ~16 KB of merged output for the failure-tail path; the rendered
`last_error.stderr_tail` is still capped at ~2 KB of bytes for
log-storage parity with 0.2.0.

This fix obsoletes the in-place cluster patch that was applied during
the 2026-04-26 session (`capture_output=True` line removed). The
upstream version preserves both halves of the contract: live output
to the caller AND a forensics-tail in `job.doc`.

### Added (gap 2) — `aexp queue stop <jobid>` interrupts a running job

0.2.0 had no verb to interrupt a running queued job. The only
recourse was hand-rolled `ps aux | grep ... → kill -9 <pid>`,
followed by `mark_status(job, 'failed')` via the Python API.
Dangerous: SIGKILL on a recycled pid can nuke arbitrary cluster
processes; multiple PIDs in the spawn tree (`aexp run-queued` parent
+ wrapper + inner training process) had to be killed individually.

`run_queued` now spawns the subprocess in its own session/process
group (POSIX `os.setsid` / Windows `CREATE_NEW_PROCESS_GROUP`) and
records `pid`, `pgid`, hostname, and a process-start-time fingerprint
in `job.doc["queue"]["proc"]` for the duration of the run. The
record is cleared on every exit path so a downstream `queue stop`
can't be tricked into killing a recycled pid.

`stop_queued()` (CLI: `aexp queue stop <jobid>`) reads the proc
record, refuses if the recorded host differs from this machine,
checks the start-time fingerprint to detect pid recycling, sends
SIGTERM to the process group, polls during a configurable grace
window (default 5s, override with `--grace-s`), and escalates to
SIGKILL if the runner ignores SIGTERM. `--force` skips SIGTERM
entirely.

A new `"stopped"` terminal status (added to `RunStatus`) distinguishes
operator-stops from `"failed"` (runtime crash) and `"abandoned"`
(never executed / pre-execution give-up). Validator's
`VALID_STATUSES` constant updated to recognize the new status.

### Added (gap 3) — `add_to_queue` dedupes recommit-only diffs

0.2.0's `add_to_queue` silently created a new signac job whenever
the sp differed, including when the only diff was the auto-injected
`code_commit` from a working-tree commit between two queueings.
Common footgun: queue, fix a docstring, queue again — now you have
2N functionally identical pending jobs.

`add_to_queue` (and `add_many_to_queue` via Cartesian product) now
scans existing pending entries for the same `(experiment_id, tag)`
and compares sps modulo `code_commit` and `code_dirty`. Matches
return the existing job and emit a `DuplicatePendingJobWarning`
(new) instead of creating a duplicate. Pass
`allow_dup_on_recommit=True` (CLI: `--allow-dup-on-recommit`) when
the recommit *is* the point of the new entries.

Tag-scoped: different tags = different operational queues = no
dedupe. Terminal-status entries (complete / failed / abandoned /
stopped) are not deduped against — re-running a finished experiment
is intentional, not a footgun.

### Added (side-friction) — `{sp_json_shell}` placeholder

The 0.2.0 `{sp_json}` placeholder emits raw JSON without shell
escaping. Templates that wrap it in shell quotes
(`runner_command: "python foo.py '{sp_json}'"`) break for any sp
value containing the same quote character — apostrophes in
sp.notes were the actual electricrag failure mode.

New `{sp_json_shell}` placeholder applies `shlex.quote` to the
JSON payload. Drop it in the template *unquoted* (the shell quoting
is part of what `shlex.quote` produces). POSIX-safe; Windows cmd.exe
caveat is documented (cluster is Linux, where it matters).

The original `{sp_json}` is preserved unchanged for backward
compatibility; the docstring now warns about the apostrophe trap and
points consumers at `{sp_json_shell}` for any shell-quoted context.

### Added (side-friction) — heartbeat in `run_lifecycle`

0.2.0's signac job document had a `status='running'` flag set once
at start of `run_lifecycle` and updated only on terminal transition.
Consumers using doc mtime as a liveness signal got false-stale
readings while jobs were working hard (no doc writes during inference
loops). The electricrag F.1 session lost real time to this.

`run_lifecycle` now starts a daemon heartbeat thread that touches
`doc["heartbeat_at"]` (ISO-8601 UTC) every `heartbeat_s` seconds
(default 30s; override per-call via the kwarg, globally via
`AEXP_HEARTBEAT_S` env var, or set to 0 to disable). External
liveness probes can compare `heartbeat_at` to wall-clock to
distinguish "still working" (heartbeat advancing) from "wedged"
(heartbeat stuck > N intervals ago).

The heartbeat is daemon-threaded so SIGKILL of the parent doesn't
leave it dangling; write exceptions inside the thread are swallowed
silently so a heartbeat-thread crash can't mask the real failure on
the main path.

### Added (side-friction) — `code_diff_summary` capture for dirty trees

When `code_dirty=True`, the bare `code_commit` SHA isn't a precise
reproducer — there are uncommitted changes layered on top. 0.2.1
captures a structured `queue.code_diff_summary` blob on dirty queue
adds:

- `diff_stat`: `git diff --stat HEAD` output (one line per changed
  file plus totals row).
- `modified_count`: number of modified/staged files.
- `untracked_count`: number of untracked files (forensics for the
  "did I forget to `git add`?" case).

Best-effort: capture is wrapped in try/except so a queue add never
fails because git is unavailable.

### Behavior changes worth noting

- `RunStatus` literal extended with `"stopped"`. Consumers that
  enumerate `RunStatus` values exhaustively in match statements will
  see a new lint warning until they handle it; semantically
  `"stopped"` is a terminal state alongside `"complete"`,
  `"failed"`, `"abandoned"`.
- The new `proc` field under `job.doc["queue"]` is *transient* — it
  exists only between Popen-spawn and process-wait-return. Don't
  depend on it for post-hoc analysis.
- `run_lifecycle` writes `doc["heartbeat_at"]` continually during
  runs. This is small per-write (~80 bytes ISO timestamp) but does
  bump signac doc-store I/O. Set `heartbeat_s=0` for short-lived
  in-process runs that don't need it.

### Test coverage

Queue tests grow from 58 → 79 (Linux: 80, Windows: 76). New
coverage:

- Live-stream proof: parent stdout sees runner output before
  subprocess exit (regression guard for capture_output buffer-then-
  dump).
- Stderr tail capture preserved through streaming refactor.
- Proc info recorded during run / cleared after.
- `stop_queued` no-live-proc / wrong-host / pid-recycle / SIGTERM /
  `--force` paths.
- Recommit dedupe: returns existing job + emits warning; respects
  `--allow-dup-on-recommit`; doesn't fire against terminal entries;
  scoped per tag; per-combo in sweeps.
- `{sp_json_shell}` apostrophe-safety.
- `code_diff_summary` written on dirty queue / skipped on clean.
- `run_lifecycle` heartbeat write / disable / env-var override.

`tests/test_validate.py::test_valid_statuses_constant_matches_run_status_literal`
updated for the new `"stopped"` literal.

## [0.2.0] — 2026-04-25

### Release summary

0.2.0 turns aexp from "installed-but-minimal" (0.1.x shipped the
H→E→F chain enforcement, signac run store, and wandb adapter)
into an actually-usable research harness. Major themes, top-down:

- **First-class artifact creation API.** `aexp.artifacts` ships
  `new_hypothesis` / `new_experiment` / `new_finding` /
  `new_thread` with automatic parent-backlink patching. Templates
  are rendered from the package (not from stale local copies), so
  creation and validation can't disagree about what "the template
  is." No more hand-writing frontmatter + remembering wikilink
  rules on every artifact.

- **Threads (`T###`) — new artifact kind.** Forward-looking
  research concerns broader than a single hypothesis. Parallel to
  H/E/F but deliberately outside the H→E→F enforcement chain.
  Spawns hypotheses via `aexp new-hypothesis --thread T###`;
  closes via `aexp close-thread T###` with a `--promoted` variant
  when the thread persists as parent context.

- **Queue + runner-script materialization.** `aexp queue add` /
  `queue run` turns declarative intent into a runner script
  (shell / slurm / manual) that executes wherever the user's
  compute environment lives — designed for agent-on-laptop,
  training-on-cluster workflows. Includes sp resolution via named
  `conditions:` blocks in experiment frontmatter (drift-proof
  provenance) and a `--sweep "KEY=a|b,SEED=0..3"` Cartesian-product
  grammar.

- **Wandb init-ownership decoupled.** Three first-class modes:
  `tracked_run` (managed), `prepare_tracker` (bring your own
  `wandb.init`), and the existing `bind_tracker` adapter path.
  Consumers with pre-existing `wandb.init` sites (e.g. electricrag's
  `loop.py`) can adopt aexp's discipline without refactoring.

- **Validator strictness.** `kb_validate` now enforces template-
  header presence (`missing_template_header`) and a
  `conditions_schema` check on experiment frontmatter. Templates
  grew `## Caveats` + `## Intent` (dual-mode pre-registered vs.
  exploratory, to kill the fabricate-retroactive-thresholds trap)
  and renamed old `## Expected Outcome` / `## Analysis` sections
  to clarify the E-run-level ↔ F-generalizable-claim boundary.

- **Install safety.** `aexp install --force` preserves user-authored
  `kb/` + `templates/` content while refreshing tooling files.
  Reported as the CHALLENGE.md-clobber bug by a real user;
  regression-guarded with a new `preserved_user_modified` action
  kind.

- **Slash commands: 3 → 18.** Artifact creation (H/E/F/T + new-run),
  finding creation (picked by what the finding cites —
  from-run / from-batch / placeholder), read/inspect (show-run,
  show-batch, list-runs, list-threads, show-thread, status,
  validate), queue lifecycle (add/list/materialize), thread
  lifecycle (new/list/show/close). Old close-run / close-batch /
  new-finding renamed to the parallel `finding-from-<source>` form.

- **Hooks tightened.** `SessionStart` silenced on missing
  `CHALLENGE.md` / `ACTIVE.md`. `enforce_hef_chain` extended to
  block hypotheses that reference a non-existent thread.

- **Docs.** New `docs/queue.md`, `docs/threads.md`. `docs/tracker-adapters.md`
  rewritten to stop implying the adapter is the canonical wandb
  surface. `docs/cli.md` + `docs/quickstart.md` updated throughout.

Full detail — including every surface, every behavior change, and
pointers to the bug reports that drove each fix — in the sections
below.

### Changed (thread docs — refinements from the first real use)

Reports back from the electricrag-side agent after salvaging two
threads under the new schema (commit `906809e` in electricrag on
2026-04-25). The schema held up; three doc-level clarifications
landed based on friction encountered:

- **Status semantics made stricter + explicit.** The electricrag
  agent defaulted to ``EXPLORING`` for salvaged drafts because "the
  work had been thought about before parking." Kaden corrected to
  ``PROPOSED``. The distinction: ``PROPOSED`` means the thread
  exists but no concrete work is underway; ``EXPLORING`` means
  someone is actively running a baseline / reviewing literature /
  pursuing a promotion criterion — work-in-flight, not intent.
  Writing a thread down doesn't promote it; only actually-running
  work does. Clarified in the thread template blockquote metadata,
  in ``docs/threads.md`` (expanded lifecycle section + new Idiom
  "Default to PROPOSED on creation"), and in the
  ``/aexp-new-thread`` slash command's flow guidance.
- **Cross-linked-artifact create-then-link ordering documented.**
  The electricrag agent hit a ``kb_write_guard`` block when T001's
  first write included ``[[T002]]`` before T002 existed. Fixed
  easily in practice (write both skeletons first, then add the
  cross-links in a second pass) but undocumented. New section in
  ``docs/threads.md`` spells out the pattern; ``/aexp-new-thread``
  flow guidance flags it inline. Applies to any cross-linked
  artifact pair, not just threads.
- **`## Sub-questions` vs. `## Promotion criteria` edge case.**
  When a sub-question has a specific prerequisite ("run a baseline
  first"), it can blur with the thread-wide promotion criteria
  section. New rule-of-thumb in ``docs/threads.md``:
  prerequisite-to-*any*-promotion goes in ``## Promotion criteria``;
  prerequisite-to-*this-specific-sub-question* stays with the
  sub-question. Minor clarification — both threads in the salvage
  test surfaced the pattern, neither was blocked by it.

No code changes. Template + docs + one slash command.

### Added (threads — new artifact kind for forward-looking research concerns)

- **`T###` artifact kind**, parallel to H/E/F. A thread is a
  forward-looking research concern broader than a single hypothesis
  — exploration that may spawn 2–5 ``H###`` over its lifetime.
  Solves the gap reported by the electricrag-side agent on
  2026-04-24: H/E/F assume you already know which hypothesis to
  write; threads capture the upstream exploration without rotting in
  session notes or going off-graph in external trackers.
- **Lifecycle**: ``PROPOSED → EXPLORING → PROMOTED`` (one or more
  hypotheses spawned; thread persists as parent context) or
  ``CLOSED`` (decided not to pursue / superseded / out of scope).
  Status transitions are manual — no implicit state machinery.
- **Linkage**: hypotheses gain an optional ``thread:`` frontmatter
  field. When set:
  - The validator (``enforce_hef_chain`` PreToolUse hook + the
    ``kb_validate`` ``reference`` check) requires the named ``T###``
    to exist on disk.
  - The thread's ``## Links`` section is auto-patched with the
    hypothesis backlink (``- [[H###]]``).
  - ``required_links_for(H)`` adds the thread to the required-link
    set, satisfied automatically by the auto-patch.
  Threads themselves are NOT in the H→E→F enforcement chain —
  hypotheses without a thread parent are still fine.
- **`aexp.artifacts.new_thread(title, ...)`** creates a validator-
  clean skeleton at ``kb/research/threads/T###-<slug>.md`` with the
  shipped template's seven required sections (`## Statement`,
  `## Sub-questions`, `## Promotion criteria`, `## Open links`,
  `## Notes`, `## Conclusion`, `## Links`).
- **`aexp.artifacts.close_thread(thread_id, conclusion=..., new_status=...)`**
  transitions the status, updates `last_updated`, and rewrites the
  ``## Conclusion`` section. Default `new_status` is ``CLOSED``;
  pass `new_status="PROMOTED"` (or `--promoted` on the CLI) when
  the thread persists as parent context for spawned hypotheses.
- **`aexp.artifacts.new_hypothesis(thread_id="T###", ...)`** —
  promotion path. Records ``thread: T###`` in H frontmatter,
  auto-patches the thread's ``## Links``, validates T existence
  before signac job creation. The thread's status doesn't auto-
  transition — call `close_thread(..., new_status="PROMOTED")` or
  hand-edit.
- **CLI verbs** (4 new): ``aexp new-thread``, ``aexp list-threads``,
  ``aexp show-thread``, ``aexp close-thread``. Plus ``--thread`` flag
  on ``aexp new-hypothesis`` for promotion.
- **MCP tools** (4 new): ``new_thread``, ``list_threads``,
  ``show_thread``, ``close_thread``. ``new_hypothesis`` gains a
  ``thread_id`` parameter.
- **Slash commands** (4 new): ``/aexp-new-thread``,
  ``/aexp-list-threads``, ``/aexp-show-thread``,
  ``/aexp-close-thread``. ``/aexp-new-hypothesis`` updated to mention
  the ``--thread`` flag for promotion.
- **Validator** (`kb_validate`): T added to ``CORE_ARTIFACTS`` with
  required fields ``("Status", "Created")``. Required-template-header
  check (`missing_template_header`) covers all seven thread sections.
  H's optional `Thread` reference is validated by `validate_ref`
  (existence) and `validate_backlinks` (bidirectional).
- **`docs/threads.md`** — canonical reference for the model:
  when-to-use, lifecycle, required sections, linkage, command
  surface, Python API, idioms (e.g. "thread titles describe the
  *concern*, not the *answer*").

Total slash commands: 14 → **18**. Total artifact kinds: 6 → **7**.

### Fixed

- **Artifact creation and validation now read from the same template
  source.** Previously `aexp.artifacts._load_template` preferred the
  consumer's repo-local `templates/<kind>.md` and fell back to the
  vendored copy; the validator's `missing_template_header` check
  always read vendored. When local templates fell behind shipped
  (which is the default state any time package templates evolve),
  creation rendered the old skeleton while validation rejected it
  for missing the new shipped headers. Reported by the electricrag-
  side agent on 2026-04-24 after `mcp__aexp__new_experiment` and
  `mcp__aexp__new_finding` produced skeletons missing `## Caveats` /
  `## Intent` / `## Outcome Summary` and the PostToolUse hook
  rejected the writes — even though the install-preserve fix was
  doing exactly what it should (preserving on-disk templates).

  Fix: `_load_template` always reads from the package-shipped
  templates at `src/aexp/vendor/limina/templates/`. The local
  `templates/` directory is now purely a *reference copy* — preserved
  across re-installs by the existing install-preserve logic, but
  never consulted by the artifact-creation API. The previous
  implicit "local file = override" semantic is removed.

  **This is a breaking change to undocumented behavior.** Any
  consumer who was relying on local-template overrides for
  customization will find their overrides silently ignored. No such
  consumers are known; the harness is pre-release. If per-project
  template overrides become a real need, the likely shape is a
  `--template-file <path>` CLI flag and a `template_path=` Python
  kwarg — file an issue describing the use case.

  3 new tests in `tests/test_artifacts.py` pin the new behavior:
  bogus content in a local `templates/hypothesis.md` doesn't leak
  into rendered artifacts; freshly-rendered E and F skeletons satisfy
  `kb_validate.validate_kb` without any post-creation edits (the
  creation-validation agreement contract).

### Added (templates + validator: stick to the templates precisely)

- **`kb_validate.validate_required_headers`** — every artifact must
  contain every top-level (`## `) header declared in the corresponding
  vendored template. Issue code `missing_template_header`. Headers
  parsed from `src/aexp/vendor/limina/templates/<kind>.md` once and
  cached. ``## Links`` excluded (covered comprehensively by the
  existing `validate_links`). Currently enforced for H / E / F; L /
  CR / SR not yet checked. Reported by the electricrag-side agent on
  2026-04-24 after deleting `## Expected Outcome` and `## Analysis`
  from an experiment file passed validation cleanly.
- **Experiment template gains `## Caveats`** between `## Procedure`
  and `## Intent`. Top-level visibility for known limitations,
  instrumentation gaps, and deviations from plan. ``_None._`` is a
  valid body for fully-instrumented runs but most experiments
  accumulate something worth recording at the top level.
- **Finding template gains `## Caveats`** between `## Evidence` and
  `## What Improved For Real`. Distinct from `## Remaining Debt`:
  caveats are about what limits *interpretation* of the finding;
  remaining debt is about what's still a workaround in the *system*.

### Changed (templates: stop nudging authors toward dishonesty)

- **Hypothesis template `## Test Plan` is now dual-mode.** Ships two
  alternate sub-blocks — *pre-registered* (confirm/reject thresholds)
  and *exploratory* (a one-line purpose, no fabricated thresholds).
  Authors pick the framing that's actually true and delete the other.
  Previously the section unconditionally asked for confirm/reject
  criteria, which trained authors to fabricate thresholds for runs
  that were really smoke tests — Kaden caught this on 2026-04-24
  with H001's first author here.
- **Experiment template `## Expected Outcome` renamed to `## Intent`**
  and rewritten with the same dual-mode pre-registered / exploratory
  structure. The "Expected Outcome" name presupposed a confirm/reject
  frame; "Intent" accommodates both honest framings without forcing
  one.
- **Experiment template `## Analysis` renamed to `## Outcome
  Summary`.** The boundary is now explicit in the template body and
  the slash-command guidance: `## Outcome Summary` reports
  experiment-level observations (*what happened in this specific
  run*); generalizable claims belong in the linked Finding's prose,
  not the experiment. Resolves the semantic overlap the
  electricrag-side agent flagged where E's Analysis and F's claim
  prose competed for the same content.

### Changed (slash commands)

- **`/aexp-new-experiment`** — flow guidance rewritten to walk
  through `## Caveats`, `## Intent` (with the smoke-test trap
  flagged), and the `## Outcome Summary` ↔ Finding-prose boundary.
  Adds a step pointing at `missing_template_header` as the validator
  signal for "you deleted a section."
- **`/aexp-new-hypothesis`** — same dual-mode `## Test Plan`
  guidance. Spells out: don't fabricate retroactive thresholds for
  exploratory runs.
- **Finding slash commands** (`from-run`, `from-batch`,
  `placeholder`) — updated to include `## Caveats` in the
  fill-in list, with the caveats-vs-remaining-debt distinction
  inline so authors don't conflate them.

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
