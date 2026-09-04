# Judge Benchmark Harness — implementation plan (v1: Tests A & B)

**Status:** plan only — nothing implemented. Written 2026-09-04 against nested repo commit `3a5035d` (v1.5.42).
**Audience:** a fresh Claude Code session with NO access to the conversation that produced this plan. Everything needed is in this file plus the repository. Where this plan conflicts with the code at your HEAD, **the repository wins — document the discrepancy in your task file and adapt.**

## 1. Executive summary

Build a **dev-only, reproducible benchmark harness** that runs multiple judge configurations (provider/model/effort) against the **same frozen historical review inputs** drawn from real playbook tasks, and scores them on valid findings, unique valid findings, severity, false positives, resource use, and latency. v1 must make two paired experiments easy: **Test A** (codex Sol medium vs high) and **Test B** (grok 4.6 medium vs high). The design must not block future Tests C (Gemini 3.8 Flash vs Sonnet 5 vs Grok), D (Astra), E (Luna screening), F (driver-vs-delegated implementation), H (implementation-source independence).

**Key architecture decision (rationale in §7):** the harness lives as **dev-only tooling in the repo root (`bench/`), modeled on the existing `arena/` precedent** — it is not a shipped `tasks` subcommand, it does not touch the production plugin surface, and therefore it needs no feature-freeze exception and no readme-audit surface changes.

## 2. Problem statement

Public benchmarks (DeepSWE, Coding Agent Index, SWE-rebench, FrontierSWE) measure task-solving agents, not playbook judging. Playbook judges receive a prepared diff + spec + context and must find defects. Seat decisions (Sol medium vs high; grok medium vs high; whether Gemini earns a seat; whether Sonnet's seat pays) are currently made from proxies. The project's own doctrine — measure, then decide — requires internal paired data.

## 3. Goals

1. Frozen, leakage-free review inputs reconstructed from historical tasks.
2. Run N candidate seat configs against the same case; collect per-invocation quality + resource + latency data.
3. Adjudication workflow producing per-case, per-candidate scores against ground truth derived from historical triage.
4. Comparison report (terminal + markdown + JSONL raw).
5. Resumable, partial-failure-tolerant runs; DNF distinct from a bad score.
6. Everything testable without spending provider quota (fake runner).

## 4. Non-goals (v1)

- No production plugin changes (no new `tasks` subcommands, no models.json schema change, no panel changes).
- No automatic semantic-equivalence NLP/embeddings — deterministic keys + human adjudication.
- No composite "MJV" score baked in — persist raw measurements; composites computed at report time and clearly labeled.
- No Gemini/Astra/Luna integration work (design must merely not block it — §26).
- No dynamic panel routing, no scheduler.

## 5. Current repository architecture (verified at `3a5035d`)

All paths relative to repo root `playbook-plugin/`; the canonical plugin tree is `plugins/playbook/`.

- **Judge/panel execution:** `plugins/playbook/tasks/review.py`
  - `cmd_panel_review` (≈:814) — parses `--mode`, `--models a,b`, `--prompt "..."` (taskless consultation), `--bare` (strips repo/mind-map context), `--no-mind-map`, `--web-search`, `--timeout`, `--soft-timeout`, `--budget`. Usage string at ≈:873.
  - `cmd_single_review` (≈:1703) — plan-review/impl-review/legacy `judge` (mode auto-detect ≈:1846). Sets `PLAYBOOK_SESSION_ID="judge"` (≈:1990).
  - Seat/effort handling: `_seat_with_effort` (≈:98); judge spec parsing `resolve_judge_spec` + `load_judge_config` in `plugins/playbook/provider/sandbox.py` (≈:208, :227). Seat spec grammar: `provider:model:effort` / bare alias (e.g. `opus`, `codex:gpt-5.6-sol:high`).
  - Status/usage extraction: `_judge_status` (≈:112), `_parse_judge_usage` (≈:134) — reuse both.
  - Timeouts: `resolve_review_soft_timeout` / hard floor via config `review_timeout_secs` (floor 1200s — passing `--timeout` below the floor is clamped UP; the harness must account for this).
  - Context budgets: `resolve_review_context_chars(project_path, stdin=bool)` (≈:977) — argv vs stdin transport budgets exist; grok argv limit ≈30KiB is a known parked issue.
  - Findings write-back: `_findings_markers`/`_write_review_findings` (≈:514–:534); panel rounds stack into per-task `judge.md`, overflow archived verbatim to `judge-archive.md` (never dropped, since 1.5.41).
  - Tamper guard: `_snapshot_repo_state` (≈:357) / `_detect_tamper` (≈:621) — panels are voided if the tree moves; harness runs must therefore use isolated worktrees, never the live workspace tree.
  - Tail-cert single-judge machinery: `_run_tail_cert_judge_raw` (≈:1590), `run_tail_cert_judge` (≈:1623) — a clean precedent for a *direct, prompt-only judge invocation* outside the panel path. **This is the closest existing shape to what the bench runner needs.**
- **Spend records:** `plugins/playbook/scripts/pb_journal.py::append_review` (≈:114) — one O_APPEND line, `hook="review"`, `decision="record"`, fields: kind (`panel|single|tail-cert`), seat, task, round_no, duration_ms, status (`ok|fail|timeout|dnf`), usage (`{status:"known",in,out}` or unknown). Contract doc: `docs/enforcement-journal.md`. The bench should emit records with a distinct `kind="bench"` **written to the bench run directory's own journal file, NOT the workspace `.agent/journal/`** (keep production telemetry clean).
- **Adapters:** `plugins/playbook/provider/adapters/{claude,codex,grok,antigravity,pi}.py` under `provider/adapter.py` ABC; sandbox execution via `provider/sandbox.py` (bwrap/seatbelt, judges read-only). **Mirror rule:** `plugins/playbook/scripts/lib/provider/` is an rsync mirror of `provider/` — the bench must import the canonical package and should not require provider changes at all in v1.
  - **agy (Google) adapter reality check** (`adapters/antigravity.py` docstring, verified): agy v1.0.2 **rejects `-m/--model`** — the judge runs whatever model the agy UI is set to (default Gemini 3.5 Flash). Direct consequence for Test C recorded in §26.
- **Severity taxonomy:** no structured field exists. Severity lives as prose conventions in judge outputs and triage: `Critical` / `Important` / (implicitly `Minor`), used consistently across judge prompts and triage sections (grep `Critical` in review.py and any task.md). **Reuse this three-level vocabulary; do not invent a new one.**
- **Findings structure:** free text. Ground truth lives in each task's `task.md` triage blocks (headed like "Triage — impl panel ROUND N", "**ACCEPT**/**PARK**/**REJECT**" prose) and `judge.md`/`judge-archive.md` rounds. There is **no machine-readable finding schema** — the corpus builder must extract triage outcomes semi-manually (§10).
- **CLI conventions:** `plugins/playbook/tasks/cli.py` `COMMANDS` tuple (≈:28) + lazy if/elif dispatch; help-coverage test pins every token (`tests/test_cli_dispatch.py`). Adding production commands triggers doc-drift guards (`test_config_doc_drift.py` for config keys — registry `HONORED_KEYS`). **Avoided entirely by the dev-only `bench/` decision.**
- **Dev-only precedent:** `arena/` at repo root — replay corpus + offline tests, never shipped, own README, invoked as plain python scripts, pre-push hook optional. The bench copies this pattern.
- **Verify contract:** `scripts/verify` (9 checks: unittest discover over `tests/`, fixtures, ledger, parity, JSON/AST/bash/lint). Bench code at repo root must keep `ast.parse @ py3.10` green (it sweeps all tracked `.py`) and its tests must be hermetic (see `tests/test_intent_ancestry_isolation.py` and `tests/test_journal_ancestry_isolation.py` for the established ancestry-isolation harness pattern).
- **Historical corpus sources on this machine (read-only to the bench):**
  - Plugin workspace: `~/Documents/Workspace/playbook-plugin-dev/.agent/tasks/` — tasks 001–045+, each with `task.md`, most with `judge.md` (+`judge-archive.md` on long arcs), receipts embedding commit SHAs (`### <ts> · risk <r> · commit <sha>` lines) and panel tree-state descriptors (since task 036).
  - HowFar workspace: `~/Documents/Workspace/HowFarAI-v2/.agent/tasks/` — tasks 001–022, richer product diversity (server/UI/perf), app repo `HowFarAI-v2/HowFar` (public GitHub).
  - Code repos for diff reconstruction: this repo and `HowFarAI-v2/HowFar` (both have full history; receipts give the anchor SHAs).

## 6. Existing reusable infrastructure (reuse, don't rebuild)

1. `resolve_judge_spec` / `load_judge_config` — seat spec parsing.
2. Adapter classes + `provider.sandbox` run path — sandboxed, read-only judge execution (the tail-cert raw runner ≈:1590 is the template).
3. `_judge_status` + `_parse_judge_usage` — status/usage extraction from CLI output.
4. `append_review`'s record shape — copy the envelope for bench JSONL (do not write to production journals).
5. `resolve_review_context_chars` — transport budgets (argv vs stdin) per provider.
6. Ancestry-isolation test harness patterns in `tests/test_*_ancestry_isolation.py`.
7. `arena/` structure/README as the dev-tool template.

## 7. Proposed architecture

```
bench/                          (repo root, dev-only, like arena/)
  README.md                     purpose, quickstart, safety notes
  judgebench.py                 CLI entrypoint: corpus | run | adjudicate | report
  lib/
    cases.py                    case model, corpus load/validate/freeze
    package.py                  frozen review-package construction + leakage filter
    runner.py                   candidate invocation (real via adapters; fake for tests)
    records.py                  run JSONL read/write (result + spend + latency)
    scoring.py                  adjudication file handling, equivalence keys, aggregation
    report.py                   terminal + markdown rendering
  corpus/
    corpus.json                 frozen case index (versioned, committed)
    cases/<case-id>/
      case.json                 metadata (below)
      spec.md                   reconstructed pre-review task spec
      diff.patch                exact reviewed diff
      context/                  optional extra frozen artifacts (test output etc.)
      truth.json                ground-truth findings (from historical triage)
  runs/                         gitignored; one dir per run
    <run-id>/
      manifest.json             full reproducibility metadata (§9 list)
      <candidate>/result.jsonl  raw judge output + status + usage + timing
      adjudication.json         human decisions (written by `adjudicate`)
      report.md                 generated comparison
```

**Why dev-only instead of a `tasks` subcommand:** (a) no production surface → no feature-freeze exception, no readme-audit/doc-drift surface, no ledger claims; (b) arena precedent exists and shipped nothing; (c) the harness's consumers are the owner + coordinator, not plugin users; (d) promotion to a shipped command later is trivial if it earns it.

**Execution primitive decision (implementer confirms):** the runner invokes adapters **directly** (the tail-cert pattern: build prompt → adapter/sandbox run → capture stdout), NOT via `tasks panel-review --prompt` — reasons: full control of the frozen input (panel path injects steering/frames), no writes into any `.agent/`, no tamper-guard interaction, no quorum semantics, per-candidate isolation. If direct adapter reuse proves harder than expected, fallback: `tasks panel-review --prompt --bare --models <seat> --no-mind-map` run with cwd inside a disposable project dir — acceptable but second choice (document which was chosen).

## 8. Benchmark case / corpus format

`case.json`:
```json
{
  "id": "pb-036-tailcert",
  "source": {"workspace": "playbook-plugin-dev", "task": "036", "repo": "playbook-plugin"},
  "repo_base_sha": "<sha the review ran against>",
  "diff_of": "<sha or range that produced diff.patch>",
  "kind": "feature|bugfix|refactor|docs|perf",
  "area": "enforcement|server|ui|tests|docs",
  "difficulty": "easy|medium|hard",
  "truth_version": 1,
  "notes": "anything a scorer must know"
}
```
**Selection criteria (target 16, min 12 for v1):** stratified — 5 plugin enforcement-code tasks, 4 HowFar server, 3 HowFar UI/client, 2 docs/assertive, 2 low-finding "clean" tasks (false-positive bait). Mix of finding-rich (≥3 accepted findings) and clean tasks; include at least 2 tasks whose panels caught a Critical (e.g. the NaN parity fail-open in HowFar task 015's arc; the risk-shadow Critical in plugin task 032) and 2 with zero accepted findings. **Exclusions:** tasks whose reviewed tree can't be reconstructed (missing SHA anchors), tasks under 10 lines of diff, and the tail-cert-only closes (no panel ground truth). **Bias guard:** select before running any candidate; freeze `corpus.json` with a version number; additions later bump the version and never retro-edit existing cases.

## 9. Frozen judge input construction

Per case, `package.py` builds the exact text given to every candidate identically:
1. `spec.md` — task.md **up to and excluding** the first review section: keep Status/Risk/Intent/Why/References/Design Phase/Work Plan gates *as they stood pre-review* (reconstruct by taking the current task.md and cutting at the first `## Plan Review`/`## Implementation Review`/findings-marker heading; where gates carry post-review outcome notes that leak verdicts, strip the note text after the checkbox line — implementer writes this filter with tests, §21).
2. `diff.patch` — `git diff <base>..<reviewed>` from the source repo, or the receipt-anchored commit's diff.
3. Optional `context/` files (e.g., test output frozen at review time when recoverable; else omitted for ALL candidates equally).
4. A fixed judge instruction template (`bench/lib/templates/judge_prompt.md`, version-stamped) modeled on the production impl-review framing (severity vocabulary Critical/Important/Minor; require file:line evidence; forbid restating the spec).
5. Repository state: a **temporary detached worktree at `repo_base_sha`** (`git worktree add --detach`), mounted read-only into the judge sandbox exactly like production judges; removed after the run.

**Reproducibility manifest (`manifest.json`) records:** corpus version, case ids, prompt template version + sha256, playbook repo SHA, source repo SHAs, per-candidate {provider, agent CLI + version (`claude --version`, `codex --version`, `grok --version`), model, effort}, timeout config, sandbox mode (ro/no-net flags), web access (off), retry policy, timestamps, host os. Stochasticity is acknowledged: runs are *auditable and comparable*, not deterministic; the manifest is what makes two runs interpretable.

## 10. Ground truth & adjudication

`truth.json` per case, extracted **manually once** by the corpus builder from historical `task.md` triage + `judge.md`/`judge-archive.md`:
```json
{"findings": [
  {"id": "T1", "file": "plugins/playbook/tasks/core.py", "symbol": "extract_risk",
   "failure_mode": "fenced-heading shadow → wrong risk class",
   "severity": "Critical", "historical_outcome": "accepted+fixed"}
], "known_rejects": [
  {"id": "R1", "claim": "…", "why_rejected": "…"}
]}
```
- **Valid finding:** matches a `truth.findings` entry (same file+symbol AND same failure mode) — outcomes `accepted+fixed` or `accepted+parked`.
- **False positive:** matches a `known_rejects` entry, or adjudicated as not-a-defect.
- **Novel finding (neither list):** goes to `adjudicate` for a human verdict (`valid-new | invalid | unclear`); **historical silence is never proof of invalidity** — `valid-new` counts as valid and is appended to `truth.json` with `truth_version` bumped for future runs.
- **Unique valid finding:** valid AND no other candidate *in the same run* produced an equivalent finding (equivalence = same truth-id, or for novel findings same file+symbol+failure-mode as adjudicated).
- Severity: the three-level existing vocabulary; the scorer records both the judge's claimed severity and the truth severity (mismatch is data, not an error).

`judgebench.py adjudicate <run-id>` presents unmatched findings one at a time (terminal prompt), writes `adjudication.json`. Deterministic matching first (file+symbol keys against truth entries); only the remainder needs a human. No embeddings, no LLM equivalence in v1.

## 11. Metrics / data model (persist raw, compute composites later)

Per candidate per case (`result.jsonl`, one line): case id, seat spec, status (`ok|timeout|dnf|malformed`), raw output path, extracted findings list `[{file, symbol?, claimed_severity, text}]` (extraction = deterministic parse of the required output format the prompt template enforces: a numbered findings list with `FILE:` / `SEVERITY:` fields — template designed for parseability), timing (spawn→verdict wall-clock ms), usage (from `_parse_judge_usage`, else `unknown`), retries. Aggregations computed by `report`: valid, unique-valid, severity-weighted valid (weights 8/3/1 **as report-time parameters, not stored**), false positives, FP rate, p50/p95 latency, timeout rate, tokens (known/unknown split). **No composite MJV stored in v1** — the report may print one clearly labeled as derived with its parameters.

## 12. Resource / quota accounting

Per invocation record: provider, agent, model, effort, in/out tokens where the CLI reports them (`claude` JSON usage; codex/grok often opaque → `unknown`, never estimated), wall-clock, retry count, quota pool name (`anthropic|openai|xai|google`), API-equivalent dollars **only** as secondary metadata computed at report time from the rate table in `bench/lib/rates.py` (sourced from the 2026-09-04 research file; clearly marked non-authoritative). **Observed quota decrement:** not machine-readable for any current provider — record as unavailable; the operator MAY note before/after usage-page readings manually in the run manifest (`manual_quota_notes`).

## 13. Model configuration representation

Candidates are capability configs, not seats:
```json
{"provider": "codex", "model": "gpt-5.6-sol", "effort": "medium", "role": "judge", "label": "sol-med"}
```
Parsed/validated via the existing `resolve_judge_spec` grammar (`codex:gpt-5.6-sol:medium`). No production models.json involvement.

## 14. CLI design (dev tool; python entrypoint, arena-style)

```
python3 bench/judgebench.py corpus validate
python3 bench/judgebench.py corpus show [<case-id>]
python3 bench/judgebench.py run --cases all|id,id --candidates sol-med,sol-high --run-id A1 [--resume] [--fake]
python3 bench/judgebench.py adjudicate <run-id>
python3 bench/judgebench.py report <run-id> [--md out.md] [--weights 8,3,1]
```
`--fake` uses the fake runner (tests, dry wiring). `run` prints a background-launch advisory (reviews exceed the 600 s foreground tool cap — same lesson as production). Exit codes: 0 ok, 1 completed-with-DNFs, 2 unusable (bad corpus/args).

## 15. Run lifecycle

`run`: load corpus → for each (case × candidate): if `--resume` and result line exists, skip → build worktree + package → invoke runner with per-invocation timeout (default 900 s soft/1200 hard to mirror production) → capture output/status/usage/timing → append result line → tear down worktree. Candidates within a case may run in parallel (they're independent processes) capped at 2 concurrent to respect provider windows. A provider failure marks that invocation `dnf` and continues; the run never aborts wholesale.

## 16. Persistence / result layout

As in §7. `corpus/` is committed (it contains only already-public/own-repo material — **verify no secrets in any diff before committing; HowFar diffs are from a public repo, plugin diffs are public**). `runs/` is gitignored except each run's `report.md` may be copied out manually.

## 17. Comparison / reporting

`report` renders per-run: the table (candidate × valid / unique-valid / Critical / Important / FP / FP-rate / tokens-known / p50 / p95 / timeout-rate), a per-case matrix (who caught what), and the derived-composite line with its parameters. Terminal + `report.md`. CSV deferred.

## 18. Failure / retry / resume

Provider unavailable / CLI missing / auth failure → invocation `dnf` with stderr captured; one automatic retry only on transport-class failures (nonzero exit with empty output), never on malformed content (that's data). Timeout → `timeout` status. Malformed findings section → `malformed` (kept, shown — a judge that can't follow output format is a result, not an error). Interrupted run → `--resume` continues from result lines. DNF never contributes to quality scores; report shows DNF rates separately.

## 19. Security / isolation / no-mutation

Judges run in the existing provider sandbox (read-only project mount) against **temp worktrees**, never the live workspace; bench writes only under `bench/runs/<run-id>/`; no writes to any `.agent/`; no pushes; network as production judges have it (model APIs only; `--web-search` never passed). The corpus builder is the only component reading workspaces, and it is read-only there.

## 20. Historical leakage prevention (concrete exclusion list)

Never include in any candidate package: `judge.md`, `judge-archive.md`, `judge-*.log`, any task.md content at/after the first review heading (`## Plan Review`, `## Implementation Review`, `<!-- playbook:*-review-findings -->` markers), triage blocks, `## Handoff`/`## Blocked` sections, Verification Receipts, `task-archive.md`, vetting-ledger.json, MIND_MAP diffs that postdate the review, and this plan's own `truth.json`. The package builder implements this as a deny-list filter with unit tests per artifact type (§21). The temp worktree is at the *pre-review* SHA, so post-review commits are structurally absent.

## 21. Testing strategy (no quota spent)

- Unit: case load/validate; package builder (leakage filter per artifact type — fixture task.md containing every excluded section, assert absence); diff reconstruction; prompt template render; findings parser (well-formed, malformed, empty); equivalence matcher; scoring aggregation incl. unique-valid logic with 2–4 fake candidates; report rendering snapshot.
- Runner: `FakeRunner` yielding scripted outputs (ok/timeout/dnf/malformed) — full `run`→`adjudicate`→`report` integration test offline; resume test (kill after case 1, resume, assert no re-invocation).
- Hermetic per repo convention: temp dirs only, ancestry-isolation pattern followed, no reads outside fixtures in tests.
- Real-provider execution is opt-in only (absence of `--fake` + an explicit `--live` flag; tests never pass `--live`).
- Bench tests live in `tests/test_judgebench_*.py` so `scripts/verify` runs them (they import `bench/lib` by path like `scripts/verify` path-loads product code).

## 22. Detailed implementation steps (sequential; each = red-first, verify green, one commit)

1. **Scaffold** — `bench/README.md`, `bench/judgebench.py` (argparse skeleton, `corpus validate` on an empty corpus), `bench/lib/__init__.py`, gitignore `bench/runs/`. Test: CLI help + validate-empty. 
2. **Case model + corpus loader** (`lib/cases.py`) — schema validation, id uniqueness, version field. Tests: valid/invalid fixtures.
3. **Package builder** (`lib/package.py`) — spec reconstruction cut + leakage deny-list + diff loader + template render (create `lib/templates/judge_prompt.md` v1 with the parseable findings format: numbered blocks with `FILE:`, `SEVERITY: Critical|Important|Minor`, `WHY:`). Tests: the every-excluded-section fixture; template snapshot.
4. **Findings parser** (`lib/scoring.py` part 1) — parse the template's output format defensively (malformed → status not crash). Tests incl. adversarial outputs.
5. **Runner** (`lib/runner.py`) — `FakeRunner` first (tests), then `LiveRunner`: temp worktree, sandbox invocation via the tail-cert pattern (import `tasks.review._run_tail_cert_judge_raw` shape — likely factor a small bench-local copy rather than importing a private symbol; decide and document), status/usage extraction via `_judge_status`/`_parse_judge_usage` imports, timing, one transport retry. Tests: fake end-to-end; live path unit-tested with a stubbed subprocess.
6. **Records + resume** (`lib/records.py`) — JSONL append/read, `--resume`. Tests: interrupted-run fixture.
7. **Adjudication** (`lib/scoring.py` part 2) — deterministic truth matching (file+symbol → truth id), interactive loop writing `adjudication.json`, truth append for `valid-new` with version bump. Tests: matching table; scripted stdin for the loop.
8. **Aggregation + report** (`lib/report.py`) — per §17 with `--weights` params. Tests: snapshot on fake data.
9. **Corpus build (content, not code)** — select 12–16 cases per §8 from both workspaces, reconstruct spec/diff/truth for each, commit `corpus/` v1. This is careful manual work driven by a checklist the step adds to `bench/README.md`. Secret-scan every diff before commit (`git diff` grep for keys; the repos are public but verify).
10. **Dry full rehearsal** — `run --fake` across the real corpus × 4 fake candidates; adjudicate; report. Fix ergonomics found.

## 23. Suggested commit breakdown

One commit per step above (1–8 code+tests; 9 corpus; 10 fixes), branch `bench/judge-harness` off current main, pushed, CI green per commit (bench tests ride `scripts/verify`).

## 24. Acceptance criteria

- `python3 bench/judgebench.py run --cases all --candidates sol-med,sol-high --run-id smoke --fake` then `report smoke` produces the comparison table offline.
- Leakage tests prove every §20 artifact class is absent from packages.
- A live single-case, single-candidate smoke (`--live`, one cheap seat) completes and records status/usage/timing (operator-run, not CI).
- `scripts/verify` 9/9 green; no production plugin file modified (allowed exceptions: none; even docs untouched — this plan file itself is the only docs addition and predates the branch).
- DNF/timeout/malformed each demonstrably distinct in the report (fake-runner test).

## 25. Running Tests A and B (operator procedure, post-implementation)

```
# Test A
python3 bench/judgebench.py run --cases all --candidates codex:gpt-5.6-sol:medium,codex:gpt-5.6-sol:high --run-id testA --live
python3 bench/judgebench.py adjudicate testA
python3 bench/judgebench.py report testA --md bench/runs/testA/report.md

# Test B (same corpus, same procedure)
python3 bench/judgebench.py run --cases all --candidates grok:grok-4.6:medium,grok:grok-4.6:high --run-id testB --live
```
Run in the background (each live run ≈ cases × candidates × up to 20 min worst-case; expect hours — resumable). Decision rule agreed with the owner: **the costlier config wins only if its severity-weighted valid findings per 1,000 known output tokens exceeds the cheaper config's** (computed by `report`; where tokens are unknown for a provider, fall back to per-invocation count parity and say so).

## 26. Future extensions (explicitly out of v1; design already accommodates)

- **Test C (Gemini 3.8 Flash vs Sonnet 5 vs Grok):** blocked on a Gemini execution path. Verified constraint: the existing `agy` adapter **cannot pin a model** (v1.0.2 rejects `-m`; uses the UI-configured model, default Gemini 3.5 Flash — the wrong model). Options, decision deferred to the owner at Test-C time: (a) set 3.8 Flash in agy's UI config and record it in the manifest (works, weakly pinned); (b) add a bench-local direct Gemini API runner (stdlib urllib; pinnable; pay-as-you-go key). **Billing facts for the owner:** no subscription is required for Test C's volume — ~16 reviews × (~200K in + ~5K out) ≈ $3–4 total at the published API rates ($0.75/M in, $3.75/M out through 2026-12-31); the free CLI tier may also suffice but has unspecified rate limits and its data-for-training term; Google AI Pro is NOT needed; API pay-as-you-go and consumer subscriptions are separate billing systems. Purchase decision is the owner's; recommendation on file: a ~$5 API key via option (b) is the cleanest pinnable route.
- **Test D (Astra):** add the candidate spec when `codex models` lists a GPT-6 Astra id; latency/timeout-rate is a primary outcome.
- **Test E (Luna screening):** same harness, plus per-candidate *prompt template variants* (generic vs specialized scans: security/test-gap/fail-open) — the template field in the manifest already versions this; add `--template` to `run` when needed.
- **Test F/H:** different harness shape (implementation, not judging) — reuse `corpus/` case format and `records.py`, do not extend judgebench for it.

## 27. Risks and unresolved questions

1. Prompt-transport limits: grok argv ≈30KiB — large diffs need stdin transport; the tail-cert parked item ("route via stdin/--prompt-file") is related. The bench runner must pick per-provider transport using `resolve_review_context_chars`; if a diff exceeds every transport, split-or-exclude the case (record which).
2. Reconstructing pre-review specs is heuristic (task.md was edited during review rounds); the cut-at-first-review-heading rule + outcome-note stripping is an approximation — document per-case deviations in `case.json.notes`.
3. Usage tokens are `unknown` for codex/grok in some paths — the §25 decision rule's fallback must be used honestly.
4. n≈16 decides only large effects; report should print bootstrap CIs over cases (implementer may defer CIs to a follow-up; if deferred, the report must say "point estimates only").
5. Fake-vs-live drift: keep `LiveRunner` thin so `FakeRunner` coverage stays representative.
6. The severity weights (8/3/1) are placeholders; calibration from historical fix costs is a future task — reports must label them.

## 28. Handoff instructions for the next session

Work in the playbook-managed workspace `~/Documents/Workspace/playbook-plugin-dev` under full playbook discipline (one task per coherent unit — suggest: task 1 = steps 1–8, task 2 = step 9 corpus, task 3 = step 10 rehearsal; classify honestly; this is dev tooling — reversible). Branch `bench/judge-harness` off up-to-date `origin/main` in the nested `playbook-plugin/` repo. Red-first per step; `python3 scripts/verify` green before every commit; push + read your own CI to four green lanes; reviews in the background; judges per `.agent/models.json`; NO version bump, NO merge, NEVER `claude plugin` commands; provider mirror untouched (this plan requires no `provider/` changes — if you find you need one, stop and re-read §7's fallback before touching it). The corpus step reads the two workspaces read-only; never write into any `.agent/` outside your own task records. If context runs high the owner will tell you — `tasks handoff` at a boundary.
