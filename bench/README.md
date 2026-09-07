# judgebench — measuring playbook judge seats on frozen review inputs

A **development tool** for `playbook-plugin` itself, beside `arena/`. It is
**never shipped** in `plugins/playbook/` — a `playbook:init` project never sees
it, it adds no `tasks` subcommand, no config key, no models.json field.

Spec: [`docs/plans/judge-benchmark-harness.md`](../docs/plans/judge-benchmark-harness.md)
(v1 = Tests A & B infrastructure). This README is the operator's quickstart.

## What it does

Runs N candidate judge configurations (`provider:model:effort`) against the
**same frozen historical review inputs** reconstructed from real playbook tasks
and scores them: valid findings, unique valid findings, severity, false
positives, resource use, latency. Raw measurements are persisted; composites
are computed at report time and labeled with their parameters.

```
python3 bench/judgebench.py corpus validate [--transport [--platform posix|windows] [--spec-mode full|compact]]
python3 bench/judgebench.py corpus show [<case-id>]
python3 bench/judgebench.py run --cases all|id,id --candidates sol-med,sol-high --run-id A1 [--resume] [--spec-mode compact] (--fake | --live)
python3 bench/judgebench.py adjudicate <run-id> [--auto]
python3 bench/judgebench.py contamination <run-id> --history NAME=PATH…
python3 bench/judgebench.py report <run-id> [--md out.md] [--weights 8,3,1]
```

Exit codes: `0` ok · `1` completed-with-DNFs · `2` unusable (bad corpus/args).

## Run layout and semantics

```
bench/runs/<run-id>/
  manifest.json              corpus version + per-case sha256 (spec/diff/context/prompt),
                             template version+sha, playbook SHA, candidates (+CLI versions),
                             timeouts, sandbox mode, retry policy, host, manual_quota_notes
  .lock                      exclusive run lock (pid) while a `run` is in flight
  <label>/result.jsonl       one line per case for that candidate (status, findings, usage, timing)
  <label>/raw/<case>.txt     the judge's raw output
  journal/enforcement.jsonl  spend records (production envelope, kind="bench")
```

- **Candidates** are `provider:model:effort` specs (or a models.json alias), optionally
  labeled: `--candidates sol-med=codex:gpt-5.6-sol:medium,sol-high=codex:gpt-5.6-sol:high`.
  Presets `sol-med`, `sol-high`, `grok-med`, `grok-high` name the Test A/B seats. Labels are
  portable directory names: `[A-Za-z0-9._-]`, ≤64 chars, no leading/trailing dot, unique
  case-insensitively, not a Windows device name, not `journal`/`raw`/`manifest.json`.
- **Snapshots, not worktrees.** A live run reviews a `git archive <repo_base_sha>` snapshot
  in a temp dir — no `.git`, so a judge cannot `git log --all` its way into the future
  (fix commits). One snapshot per case, shared by all candidates. Pass the checkout for
  each case's `source.repo` with `--source-repo NAME=PATH` (`playbook-plugin` defaults to
  this repo); a missing mapping is a `dnf`, never a guess.
- **Transport preflight.** If ANY candidate's transport cannot carry the rendered prompt
  (grok reads it from argv; the POSIX per-argument cap and production's context budget
  apply), the case is `excluded` for ALL candidates in that run — paired inputs are never
  trimmed per candidate.
- **Statuses:** `ok` (parsed, may be zero findings) · `malformed` (ran, no parseable
  FINDINGS block — a result, not an error) · `fail` (judge exited non-zero with output) ·
  `timeout` · `dnf` (did not finish: CLI missing, spawn error, snapshot failure) ·
  `excluded` (preflight). One automatic retry only on transport-class failures.
  Exit code 1 when any `dnf`/`timeout`/`excluded` occurred.
- **Resume:** `--resume` re-runs the (case, candidate) pairs whose LAST result line is
  missing, torn (crash mid-append), `dnf` or `timeout` (transient); `ok`/`malformed`/`fail`/
  `excluded` pairs are done. History is kept in `result.jsonl`; the last line per pair is
  the current result. Resume refuses (exit 2) if mode (fake/live), the candidate set,
  either timeout, `--concurrency`, the fake script's content, or the corpus content
  (spec/diff/context/prompt hashes) changed since the manifest; cases may be a subset;
  `truth_version` is deliberately NOT a resume key (adjudicating an interrupted run must
  not block resuming it). The playbook checkout SHA and, for live runs, the provider CLI
  versions must also match — the harness is part of the instrument. A run id with results cannot be re-launched without `--resume`.
  Two launchers on one run id: the second is refused by the lock. Run ids are one safe
  path segment (`[A-Za-z0-9._-]`, no leading dot).

## Adjudication and report

`adjudicate <run-id>` first records every DETERMINISTIC match — a finding whose
normalized `(file, symbol)` hits exactly one `truth.findings` entry (→ valid) or
one keyed `known_rejects` entry (→ false positive). Collisions (two truth entries
in one symbol) and novel findings go to you, one at a time:

```
m <truth-id>   same defect as that truth entry (this is how equivalence classes are assigned —
               two candidates phrasing one defect differently both map to one id)
v              valid-new: appended to the case's truth.json (deduped by file+symbol+failure mode),
               truth_version bumped in truth.json AND case.json — historical silence is never
               proof of invalidity
r <reject-id>  matches a known reject (false positive)
i / u / s / q  invalid (false positive) / unclear (stays pending) / skip / quit-and-save
```

`--auto` records only the deterministic pass (what the offline smoke uses).
Decisions are saved after every answer in `adjudication.json`. Adjudication holds
both the run lock and a corpus lock (`<corpus>/.lock`) — Test A and Test B share one
corpus, so they are adjudicated one at a time. `case.json` is the sole authority for
`truth_version`.

`report <run-id> [--md out.md] [--weights 8,3,1]` renders per candidate: invocations by
status (`ok` / `malformed` / `fail` / `timeout` / `dnf` / `excluded` — each its own column,
so a DNF never looks like a bad score), valid, unique-valid (held by exactly one candidate
in the case), valid by TRUTH severity, false positives + FP rate, pending, severity-weighted
valid, tokens known/out, weighted per 1,000 known output tokens (the plan §25 decision rule),
p50/p95 wall-clock, timeout/DNF rates, and a USD estimate only where both usage and a rate
exist; then a per-case matrix of who caught what. The composite line is labeled with its
weights and "point estimates only" (no bootstrap CIs in v1). Nothing derived is stored.

## Safety notes (read before `--live`)

- **Real providers are opt-in.** `run` refuses unless exactly one of `--fake`
  (scripted runner, free) or `--live` (real CLIs, spends quota) is given. Tests
  never pass `--live`.
- Judges run in the existing provider sandbox, **read-only**, against a
  **temporary detached worktree** at the case's pre-review SHA — never the live
  workspace tree. The bench writes only under `bench/runs/<run-id>/` (gitignored).
- **No writes to any `.agent/`.** Spend records go to the run directory's own
  journal (`bench/runs/<run-id>/journal/enforcement.jsonl`, `kind="bench"`),
  never to a production journal.
- `--web-search` is never passed. Network is whatever production judges get
  (model APIs only).
- A live run can take hours (cases × candidates × up to 20 min). Launch it in
  the background; it is resumable with `--resume`.

## Honesty notes

- **Claude candidates cannot vary effort in v1.** The claude adapter hardcodes
  `--effort high` (`provider/adapters/claude.py`); a spec like `claude:opus:medium`
  would be recorded truthfully as what actually ran. codex and grok carry the
  effort inside the model variant and honor it.
- Token usage is `{"status":"unknown"}` unless a CLI reports it in a parseable
  form (`tasks.review._parse_judge_usage`) — numbers are never estimated.
- Dollar figures in a report are **secondary metadata** from `bench/lib/rates.py`
  (dated, non-authoritative), never a measurement.
- Runs are auditable and comparable, not deterministic: `manifest.json` is what
  makes two runs interpretable.
- **Read isolation is the OS sandbox's, not the bench's.** The provider sandbox mounts
  the host filesystem read-only; a judge that deliberately reads outside its snapshot
  could reach historical `.agent/tasks/*/judge.md` on this machine. The bench mitigates
  (snapshot in a temp dir, the prompt tells the judge to stay inside its repository) but
  cannot enforce it without a `provider/` change — out of scope for v1, disclosed here.
- Snapshots carry no git history, so judges cannot `git blame`/`git log` inside them
  (production judges can). Acceptable for reviewing a diff; a known difference.
- The bench's `LiveRunner` is a thin bench-local copy of the shape of production's
  tail-cert raw runner, not an import of it (that function reads `default_judge` from
  config). Status/usage extraction IS imported from `tasks.review`.
- **A crash between a provider call succeeding and its result line being appended
  re-runs that pair on `--resume`.** Results are persisted per candidate as each one
  completes, so a crash loses at most the invocations still in flight for one case
  (≤ `--concurrency`), never a finished one. v1 has no per-pair in-flight marker; if a
  live run died mid-flight, check the provider's usage page before resuming. (Parked
  for v2.) A retried transport failure keeps its first attempt in the result line's
  `attempts` list (status, usage, duration, its own raw file) AND emits one
  spend-journal line per actual invocation — nothing billed is dropped. Every attempt
  keeps its own raw file (`<case>.<seq>.txt`). Latency (p50/p95) is the final attempt's
  wall-clock and includes timed-out invocations.
- **Unique-valid is `n/a`, not 0, whenever some candidate in the manifest has no
  scorable result for a case** — uniqueness is only defined against peers that ran.
- On Windows the argv-transport preflight applies the adapters' ~30k whole-command-line
  cap (the POSIX per-argument cap does not exist there); an adapter's own size-cap
  envelope is deterministic and is never retried.
- A lock whose holder process is provably dead (hard crash) is reclaimed automatically;
  an unreadable holder is not — delete the `.lock` by hand only when you are sure.
- **Corpus-builder duty the filter cannot do for you:** `## Design Phase` answers are
  kept verbatim because the gate discipline writes them before the plan review. If a
  Design answer was revised AFTER a review (plain wording, no verdict token), only a
  manual audit catches it — diff `task.md` against the plan-review-time commit named in
  the task's receipts when freezing a case, and record deviations in `case.json.notes`.
- A reconstructed spec that still carries review tokens (`PANEL VERDICT`, `impl-panel`,
  `judge.md`, a `CAP:` line, an H3 review/triage heading inside a kept section) makes
  `build_package` fail loud with `LeakageError` — clean `spec.md` by hand and record why
  in `case.json.notes`.

## Rehearsal and ergonomics (step 10, task 049)

- **`corpus validate --transport`** renders every case exactly as `run` would (time-budget clause
  included) and asks each Test A/B seat's ADAPTER whether its transport can carry the prompt:
  codex/claude read stdin (200k-char budget), grok reads argv (100k-char budget, POSIX 128 KiB
  per element, Windows ~30k whole line). `bench/lib/transport.py::seat_verdict` is the single
  decision and `LiveRunner.preflight` delegates to it, so the report and the run never disagree.
  The default platform is **posix** — Test B's host; `--platform windows` is an explicit,
  informational simulation (every frozen prompt exceeds grok's Windows line cap, so Test B is
  not runnable from a Windows host). Rows print `spec`/`diff` chars so you can see where the
  size is. Exit 1 when any case fails any seat. Frozen corpus v3: 19/19 fit on posix.
- **`run --spec-mode compact`** shows every candidate the spec's Intent, Why, References and
  Work Plan only (Design Phase and housekeeping dropped; the leak scan still runs). Recorded in
  `manifest.spec_mode` and every result line; `--resume` refuses a mode flip (the prompt hash
  already did — this is the clearer message). It is the fallback for a case that would not fit
  a seat, and it helps only SPEC-dominated prompts — a diff-dominated one needs a re-freeze or an
  exclusion, which is why the transport rows show the split.
- **`contamination <run-id> --history NAME=PATH…`** — live judges on this machine can read the
  historical `.agent/tasks/*/judge.md` the corpus came from. The scan compares each judge's
  LATEST raw output with that case's `judge.md` + `judge-archive.md` (history root keyed on
  `case.json` `source.workspace`, task dir resolved as `<NNN>-*` exactly once — never guessed;
  only `judge*.md` is read) for shared word 12-grams, EXCLUDING every n-gram present in the
  rendered prompt (spec, diff, template, context) so quoting the inputs is never flagged.
  Entries carry raw/prompt/history sha256; the scan holds the run lock, refuses when the rebuilt
  prompt no longer matches the run manifest's hash (the corpus moved), and writes
  `contamination.json` atomically; `report` shows `contam?` (flagged pairs; `n/a` before a scan;
  `N stale` when a raw, the prompt or the history changed after it; `N unscanned` for pairs the
  scan could not check — those are NOT clean) and marks flagged cases `!c` in the matrix. **It detects
  QUOTING, not silent influence** — a judge steered by history without copying twelve words is
  invisible to it; that residual is disclosed in every report note.
- **Rehearsal** — `bench/fake-scripts/rehearsal.json` drives four fake seats over the real corpus
  with mixed outcomes (ok with real-truth hits + novel findings, malformed, timeout, dnf, fail,
  empty). `tests/test_judgebench_rehearsal.py` runs it end to end: `run` exits 1 (timeout/dnf are
  scripted), `adjudicate --auto` credits the real hits and leaves the novel ones pending, `report`
  shows every status in its own column, the contamination scan flags nothing (the false-positive
  bound on the real corpus), and interactive adjudication with scripted stdin (`m <id>`, `v` +
  its failure-mode line, `i`, `q`) runs ONLY against a temp copy of the corpus — `v` rewrites
  `truth.json`/`case.json`, so it must never run against the frozen tree; the test asserts the
  freeze is byte-identical afterwards.

## Layout

```
bench/
  judgebench.py     CLI entrypoint
  lib/              cases · package · runner · records · scoring · report · rates
  corpus/           frozen case index + cases/<id>/{case.json,spec.md,diff.patch,truth.json,context/}
  runs/             gitignored; one dir per run
```

Tests live in `tests/test_judgebench_*.py` so `scripts/verify` runs them.

## Corpus v3 (step 9 — built 2026-09-07, task 048)

**19 frozen cases** in `bench/corpus/` (`corpus.json` version 3 — v1 and v2 were re-frozen after two implementation-panel rounds found review provenance and execution results the section filter let through; no live run ever saw v1 or v2), reconstructed from two
workspaces' historical panel reviews: `playbook-plugin-dev` (this plugin, 8 enforcement
cases) and `HowFarAI-v2` (a Next.js app reviewed with playbook; 5 server, 4 UI/perf,
2 docs). `python3 bench/judgebench.py corpus validate` → `corpus v3: 19 cases OK` — validate
BUILDS every package (leak scan + budgets), so a leaking spec fails in CI.

| stratum | cases | notes |
|---|---|---|
| plugin enforcement | pb-020-r1, pb-032-r1, pb-032-r2, pb-036-r2, pb-039-r1, pb-039-r2, pb-039-r4, pb-042-r2 | 039-r4 is a converged-tree bait |
| HowFar server | hf-007-r2, hf-007-r3, hf-007-r6, hf-011-r6, hf-011-r8 | 007-r6 is a pure bait (`findings: []`); 011-r7 was DROPPED (its round's doc accepts could not be kept in a diff under the budget — a round's truth minus its accepts does not ship) |
| HowFar UI/perf | hf-015-r1, hf-015-r2, hf-017-r2, hf-018-r4 | 018-r4: parked-only residuals |
| HowFar docs/assertive | hf-014-r3, hf-014-r4 | the retired-hosting claim class |

11 cases carry at least one Critical the historical panel caught; 3 have zero fixed
findings (bait); difficulty: 3 easy / 5 medium / 11 hard. Every rendered prompt is under
90,000 chars and 120,000 bytes (grok's argv element); `corpus validate` prints the largest.
**Plan §4.2 said target 16, minimum 12; 19 are frozen** — every case is real and checked,
but the live-run spend scales with the count, so the subset for Tests A/B is an OWNER
decision (`--cases` takes an explicit list); the two "clean bait" slots are filled by one pure
bait (007-r6) and two near-clean cases whose only truth is parked residuals (039-r4, 018-r4).

### How a case is built (deterministic, re-derivable)

The three dev tools in `bench/tools/` read the two workspaces and their git repos
READ-ONLY and write only under `bench/corpus/`:

1. `map_rounds.py --workspace WS --task N --repo PATH` prints the round↔commit
   **evidence**: judge.md rounds in FILE order (playbook prepends, so newest-first),
   task.md audit receipts chronologically, the repo's commits in the receipt window,
   and — where a `**Panel-snapshot:**` exists (plugin ≥ task 039, HowFar ≥ 014) — the
   deterministic pairing. Legacy records (trim pointers, fewer receipts than rounds) are
   flagged AMBIGUOUS; their triage lives in `task-archive.md`. The human decides.
2. `case_from_task.py … --reviewed SHA [--base SHA] [--exclude PATH] [--delete TEXT]
   [--replace OLD=NEW] --round N --fix-commit SHA --evidence "…"` writes the case dir:
   `spec.md = reconstruct_spec(task.md)` + the recorded `spec_edits`; `diff.patch =
   git diff base..reviewed` minus `diff_excludes` (lockfiles, minified/map/dist/vendored,
   binaries, plus explicit paths for the size budget); `case.json` carries the REVIEWED
   sha (the tree WITH the diff), `diff_of`, `spec_source_sha256`, and a `mapping` object
   (round ordinal, fix commits, the evidence sentence). `truth.json` starts empty.
3. The builder writes `truth.json` from that round's triage: one entry per ACCEPTED
   finding, `severity` = the triage's word, `historical_outcome` accepted+fixed (with
   `fix_commit` + `fix_evidence`) or accepted+parked; explicit REJECT lines become
   `known_rejects` (with `file`/`symbol` when the claim named them).
4. `check_truth.py --source-repo NAME=PATH… [--workspace NAME=PATH…]` is the mechanical
   instrument: `repo_base_sha` resolves and is the RIGHT endpoint of `diff_of` (base an
   ancestor); `diff.patch` re-derives byte-for-byte under pinned `git diff` flags (prefixes,
   abbrev, algorithm, renames, textconv — an operator's git config cannot change the bytes);
   the `mapping` object is present and consistent (round ≤ rounds_total, non-empty evidence,
   fix_commits ⊇ the truth's fix commits); every finding's `file` exists at the reviewed sha,
   is NOT in `diff_excludes` (a truth file must be in the diff the judge sees), and its
   `symbol` occurs in it; an accepted+fixed finding's `fix_commit` DESCENDS from the reviewed
   commit, touches the file, and its `fix_evidence` (an exact substring) is ABSENT at the
   reviewed sha, ABSENT at the fix commit's parent, and PRESENT at the fix commit — so the fix
   is provably not in the reviewed diff and is introduced by the named commit; no two cases
   share (repo, sha, diff_of); the package builds leak-free under the char and byte budgets;
   with `--workspace`, spec.md regenerates from the source record + `spec_edits`. It rejected
   several of the builder's first evidence strings (pre-existing text, wrong file, wrong
   symbol) — that is the point.

### What the spec filter redacts (v3, after two implementation-panel rounds)

`reconstruct_spec` keeps the allowlisted sections, and inside them now ALSO drops
`### Recent Chat` blocks (auto-captured session messages — one carried the owner's
"stop the panel loop" ruling), execution `- **Result:**` bullets, checked-gate outcomes
after ` → ` as well as ` — `, the WHOLE body of a checked gate that has no outcome
separator (such gates were written as execution logs — only the bold title or first clause
survives), every table row and plain paragraph inside Work Plan / Pre-review (measurement
tables, sweep-status tables and result prose live there; bold-only sub-headers, blockquotes,
list items and an open item's indented continuation are kept), any continuation line after a
checked gate up to the next real list item, and mechanically redacts inline review provenance:
`(panel opus#1/terra#4)` parentheticals, bare seat refs (`sonnet#2`, `codex:sol#4`,
`terra-crit`), plan-triage labels (`ACCEPT-C`, `ACCEPT-D/F/G`) and lettered plan-finding
refs (`(finding H, …)`), a dangling `, panel )` left by seat-ref removal and `plan panel
triaged (…)` clauses. `leak_scan` was broadened for what redaction cannot decide
(`added at impl-review`, `revised after`, `FIXED (round 2-4)`, `finding H`, an owner ruling
about a panel/round, a Recent Chat heading) — those are cleaned by hand as recorded
`spec_edits`. Redaction over-strips by design (a parenthetical that merely starts with
"panel" goes too).

### Base commit and rounds

A "feature commit" is often the POST-round-1 tree: the first panel ran on the dirty
pre-commit tree and its fixes were committed together with the feature (plugin 036 →
`0714585`, 042 → `36dda9f`, 017 → `4bb6923`). The evidence check catches this (the
round-1 fix evidence is already present), so such cases reproduce ROUND 2. Later-round
cases use `--base` = the task's pre-feature commit so the judge sees the ACCUMULATED
diff, never a lone fix commit's delta.

### Known limits (disclose in every report)

- **Defect reality is the historical panel's judgment**, read by the builder from the
  triage prose; the machine proves only fix-absence-at-review / fix-presence-after and
  that files/symbols exist. Severity labels come from the triage's words; where a
  triage gave none the builder applied the plan vocabulary and said so in `notes`.
- `spec.md` approximates the pre-review task.md via the closed allowlist filter; the
  template preamble blockquote is deleted in every case and a handful of one-phrase
  replaces neutralize domain text that trips `leak_scan` (all recorded in `spec_edits`).
  Non-template H2 sections (e.g. HowFar 015's `## Scope guardrails`) are DROPPED — the
  judge does not see them; per-case `notes` list them. Design-Phase answers were hand-
  audited for post-review edits (none found).
- The reviewed commit may differ from the exact dirty tree a historical panel saw; what
  matters — the fixes were not in it — is what `fix_evidence` proves.
- Some `diff.patch` files exclude tests, docs or perf scripts to fit the budget; those
  files are in the snapshot tree and are listed in `diff_excludes`. A judge is therefore
  not shown every changed file in such cases.
- Two truth entries on one `(file, symbol)` never auto-match (scoring needs exactly one);
  `check_truth` prints the collisions and `adjudicate` routes them to the human.
- Truth = the named round's ACCEPTED findings, so one known-real defect of a reviewed tree
  is deliberately NOT in truth and must be adjudicated `v` (valid-new) if a judge reports
  it: pb-036-r2's `__test_stub__` env seam (rejected in round 2, conceded and removed in
  round 3). The case's `notes` names it. A truth file may never sit in `diff_excludes`
  (`check_truth` fails), which is why hf-011-r7 was dropped rather than shipped short.
- `check_truth --workspace` treats a drifted source record as a FAILURE (the record no
  longer vouches for the spec); re-freeze from the current record or restore the source.
- Baits are not all pure: `pb-039-r4` and `hf-018-r4` carry accepted+parked residuals so
  a judge who finds them is credited, not penalized; only `hf-007-r6` has `findings: []`.
- No credential appears in any diff; the only password-like string is the self-host
  stack's local default DB-role password, already public in the HowFar repository.
- The corpus is FROZEN at version 1 before any run. Additions bump the version and never
  edit an existing case.
