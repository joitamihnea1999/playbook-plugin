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
python3 bench/judgebench.py corpus validate
python3 bench/judgebench.py corpus show [<case-id>]
python3 bench/judgebench.py run --cases all|id,id --candidates sol-med,sol-high --run-id A1 [--resume] (--fake | --live)
python3 bench/judgebench.py adjudicate <run-id>
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
  directory names: `[A-Za-z0-9._-]`, no leading dot, not `journal`/`raw`/`manifest.json`.
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
  re-runs that pair on `--resume`** (one extra paid invocation per crash). v1 has no
  per-pair in-flight marker; if a live run died mid-flight, check the provider's usage
  page before resuming. (Parked for v2.) A retried transport failure keeps its first
  attempt's envelope in the result line's `attempts` list AND emits one spend-journal
  line per actual invocation — nothing billed is dropped. Each attempt keeps its own
  raw file (`<case>.<seq>.txt`). Latency (p50/p95) is the final attempt's wall-clock
  and includes timed-out invocations.
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

## Layout

```
bench/
  judgebench.py     CLI entrypoint
  lib/              cases · package · runner · records · scoring · report · rates
  corpus/           frozen case index + cases/<id>/{case.json,spec.md,diff.patch,truth.json,context/}
  runs/             gitignored; one dir per run
```

Tests live in `tests/test_judgebench_*.py` so `scripts/verify` runs them.

## Corpus build checklist (step 9 — content, not code)

Filled in when the corpus is built. Until then the corpus is empty and
`corpus validate` reports `0 cases`.
