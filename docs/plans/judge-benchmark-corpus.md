# Judge Benchmark — corpus, rehearsal and live runs (plan v1, steps 9–10 + Tests A/B)

**Status:** plan only — nothing in it is implemented. Written 2026-09-05 on branch `bench/judge-harness` at nested commit `542697f` (harness v1 complete, task 046). Companion to [`judge-benchmark-harness.md`](judge-benchmark-harness.md), whose §22 steps 1–8 are DONE; this plan is its §22 steps 9–10 plus §25.
**Audience:** a fresh Claude Code session with NO access to the conversations that produced either plan. Everything needed is here plus the repository. **Where this plan conflicts with the code or the git history at your HEAD, the repository wins — document the discrepancy in your task file and adapt.** Every commit sha, size and candidate below is a POOL to re-verify, not a fact to trust.

## 1. Executive summary

The harness exists and is tested (2013-test suite, 4-lane CI green) but the corpus is empty, so it has never measured anything. Three tasks finish the job:

- **Task A — corpus (plan step 9), `assertive`.** Freeze 12–16 cases from two workspaces' historical tasks. The survey in §3 found the decisive fact: both repos commit the feature FIRST and the panel's fixes in SEPARATE later commits, so the reviewed diff, the tree the judge should see, and the ground truth are all exactly reconstructable from git plus the task record. Ground truth = the round's accepted findings whose fix is absent from the reviewed diff.
- **Task B — rehearsal + ergonomics (plan step 10), `reversible`.** A per-case size/transport report (grok reads its prompt from argv and most historical diffs are too big), a compact spec mode, a post-run contamination scan (judges run on a machine that holds the historical `judge.md` files), and the `run --fake` rehearsal over the real corpus.
- **Task C — live runs, `reversible`.** One-case smoke on a cheap seat, then Test A (codex Sol medium vs high) and Test B (grok 4.6 medium vs high) as resumable background runs, adjudication, reports, the §25 decision rule. Needs the owner's spend approval (§7).

Order: A → B → C. A and B need no owner decision. A is the context-heavy one; start it in a fresh session.

## 2. Where things stand (verify at your HEAD)

Branch `bench/judge-harness` (nested repo `playbook-plugin/`, NOT merged, no version bump), 14 commits over `origin/main` 2371177. Dev-only tooling at the repo root, never under `plugins/`:

```
bench/judgebench.py            CLI: corpus validate|show · run · adjudicate · report   (exit 0 / 1 completed-with-DNF / 2 unusable)
bench/lib/cases.py             corpus.json {version, cases[]} + case.json + truth.json schema (CorpusError names the case)
bench/lib/package.py           reconstruct_spec (allowlist section filter), deny-list, leak_scan (fail-loud), render_prompt
bench/lib/templates/judge_prompt.md   v1, production impl-review framing + trailing FINDINGS block
bench/lib/scoring.py           parse_findings · match_finding · adjudicate loop · append_valid_new · resolve_validity
bench/lib/runner.py            FakeRunner · LiveRunner (direct adapters) · classify · preflight · run_case(on_result)
bench/lib/snapshot.py          `git archive <sha>` → temp dir (no .git; the judge cannot see the future)
bench/lib/records.py           result.jsonl · manifest.json (hashes) · RunLock (atomic stale reclaim) · resume · bench spend journal
bench/lib/report.py, rates.py  aggregation (per-candidate table, per-case matrix, labeled composite), sparse non-authoritative rates
bench/README.md                operator quickstart, run semantics, honesty notes — READ IT FIRST
tests/test_judgebench_*.py     149 tests; run: python3 -m unittest discover -s tests -p 'test_judgebench_*.py'
```

Semantics the corpus builder must respect (all enforced by `corpus validate` / `build_package`):
- `case.json`: `id` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, equals the dir name and the index entry), `source{workspace, task, repo}`, `repo_base_sha` (7–40 hex — **the REVIEWED commit, i.e. the tree WITH the diff applied; that is what the judge's snapshot is built from**), `diff_of` (free string; use `"<parent>..<reviewed>"`), `kind ∈ {feature,bugfix,refactor,docs,perf}`, `area ∈ {enforcement,server,ui,tests,docs}`, `difficulty ∈ {easy,medium,hard}`, `truth_version` int ≥ 1 (the sole authority; truth.json must not carry a different one), optional `notes`.
- `truth.json`: `findings[] {id, file, symbol|null, failure_mode, severity ∈ {Critical,Important,Minor}, historical_outcome ∈ {accepted+fixed, accepted+parked, valid-new}}`, ids unique; `known_rejects[] {id, claim, why_rejected, optional file, optional symbol}` — a reject auto-matches only when it carries `file` (+`symbol`). Auto-match credits a finding only when its normalized `(file, symbol)` hits exactly ONE truth entry; a symbol-less finding only matches a symbol-less entry. Everything else goes to the human in `adjudicate`.
- `spec.md` is re-filtered at build time (`reconstruct_spec`) and then scanned: `PANEL VERDICT: PASS|FAIL`, `playbook:*-review-findings` markers, `impl-panel #3`/`F2`-style finding references, `CAP: n/n` lines, triage accept/reject/park lines, and H3+ review/triage headings make `build_package` raise `LeakageError`. Kept sections: Status, Risk, Intent, Why, References, Design Phase, Work Plan, Pre-review (checked Work Plan/Pre-review gates are reset and their notes stripped, including wrapped continuation lines). Design Phase answers are kept VERBATIM — auditing them for post-review edits is the builder's manual duty (§4.6).
- `context/` files pass a case-insensitive deny-list (`judge*.md`, `judge-*.log`, `task-archive.md`, `vetting-ledger.json`, `truth.json`, `case.json`, `MIND_MAP*.md`, `chat_log*.md`, `enforcement.jsonl`) and may not be symlinks.
- Live runs snapshot `source.repo` via `--source-repo NAME=PATH`; `playbook-plugin` defaults to the nested repo. Use `repo: "playbook-plugin"` and `repo: "HowFar"`.
- Candidate presets: `sol-med`, `sol-high`, `grok-med`, `grok-high`. `--resume` pins mode, candidate set (incl. resolved backend/variant), timeouts, concurrency, fake-script hash, corpus content hashes and the playbook checkout sha; it re-runs `dnf`/`timeout` pairs.

Not done, and out of scope for A/B/C: bench read isolation (needs a `provider/sandbox.py` change — owner decision), a per-pair in-flight marker, the merge of the branch.

## 3. Survey facts (2026-09-05; re-verify)

1. **Feature → panel → fix commits.** Plugin task 036: feature commit `0714585` (16:57, +1387 lines in core/lifecycle/review + tests), round-1 findings A–H triaged in task.md, fix commit `e032d92` (17:15) touching exactly core.py/review.py/tests. HowFar task 015: `0c4e4e5` "Add mobile performance measurement harness and audit" (Aug 29 09:52), rounds, then `11341d6` "Harden perf harness and correct audit after review" (Aug 30 01:02) and `d573731` "…after second review". The pattern holds for tasks 032, 037, 039 (V1…V10 per round), 042 (`36dda9f` → `104bc98` round 2 → `15755bc` r3 → `44ff27e` r4) and HowFar 007, 009, 014, 017, 020/022.
2. **Multi-round tasks yield extra, harder cases:** reviewed = tree after round-k fixes, truth = round k+1 accepts.
3. **Task records are final-state only** (outer workspace commits the record at close; HowFar workspace likewise). The pre-review spec comes from the filter; the round↔commit mapping comes from commit dates vs the receipts' timestamps (`### <ts> · … · commit <sha>` lines in task.md) and the `**Commit:**`/`**Panel-snapshot:**` headers in judge.md (nested-repo commits appear in the snapshot only from plugin task 039 / HowFar 014 onward; before that `**Commit:**` is the OUTER workspace commit and is useless for the nested code).
4. **Size is the binding constraint.** 036 feature diff = 114 KB; HowFar 015 = 153 KB of which a 2385-line `package-lock.json`; HowFar 015 task.md = 59 KB. grok's prompt rides argv: production budget `resolve_review_context_chars(project, stdin=False)` ≈ 100k chars, POSIX physical cap 128 KiB per argument. Under the fairness rule an oversize case is excluded for BOTH seats, which would gut Test B. Build every case to render under **90k characters**; strip lockfiles, minified/generated assets, binaries and vendored code from `diff.patch`.
5. **Repos are public; a keyword secret scan of the two sample diffs was clean** (only the word "token" in prose). Scan every diff anyway.
6. **Both workspaces' `.agent/tasks` are readable by a live judge** (the provider sandbox mounts the host read-only). Mitigations exist (temp-dir snapshot, prompt instruction); Task B adds a detector.

## 4. Task A — corpus build (plan step 9)

Classify `assertive`: the corpus asserts ground truth about historical reviews. Instrument that would show it wrong: the recipe script re-derives spec/diff from git and the task record deterministically; per-case checks (§4.7); an impl panel reads a sample of cases against their sources.

### 4.1 Tooling to write first (red-first, small)
- `bench/tools/case_from_task.py` (dev-only, stdlib): inputs `--workspace PATH --task N --repo PATH --reviewed SHA --id CASE_ID --kind --area --difficulty [--exclude GLOB…]`; outputs the case dir: `spec.md` = `reconstruct_spec(task.md)` (+ prints `leak_scan` hits so you fix the source by hand), `diff.patch` = `git diff <reviewed>^ <reviewed>` minus excluded paths (default excludes: `*lock*.json`, `*.min.*`, `*.map`, `dist/**`, `node_modules/**`, binaries via `--numstat` `-` rows), `case.json` with `repo_base_sha=<reviewed>`, `diff_of="<parent>..<reviewed>"`, and a `truth.json` SKELETON (`findings: []`, `known_rejects: []`) plus a `notes` line naming the round/commit mapping. Prints rendered prompt size in chars and bytes. Tests: a temp git repo + fixture task.md.
- `bench/tools/map_rounds.py`: for a task dir + repo, list panel rounds (from judge.md + judge-archive.md: `# Panel Impl Review`, `**Commit:**`, `**Panel-snapshot:**`, receipt timestamps) alongside the repo's commits in the task's window, so the round↔commit mapping is a printed table you confirm by hand, not a guess.
- Both live under `bench/tools/`, are covered by `tests/test_judgebench_tools.py`, and read the two workspaces READ-ONLY. Never write into any `.agent/` outside your own task record.

### 4.2 Candidate pool (stratified per harness plan §8; verify each anchor with `git show --stat`)

| stratum | task | reviewed commit (round 1) | fixes in | notes |
|---|---|---|---|---|
| plugin enforcement | 036 tail-certification | `0714585` | `e032d92` | truth A–H, several Critical; a 2nd case from a later round is possible (`e032d92`→`2064484`, …) |
| plugin enforcement | 037 session-pointer | `70fa74a` | `8400e02`, `41f6ccf`, `d14d9c1` | rounds 2/4/6 have fix commits |
| plugin enforcement | 039 fence consolidation | `34d3ce2` (V1) and `a0f86a3` (V8) | V2… / `f0f559c`, `8d58d23` | two cases: round 1 and round 2 |
| plugin enforcement | 042 spend journal | `36dda9f` | `104bc98`, `15755bc`, `44ff27e` | panel-snapshot present |
| plugin enforcement | 032 P1 fence-blind | `24fcc91` | `0168ecd`, `ab1649c`, `1a51624` | Critical risk-shadow |
| plugin docs/assertive | 013 document config keys | find via task record | — | docs case |
| plugin bait | 020 handoff (`3c261ef`→`eefadee`) or 038 | — | — | only if the round accepted nothing behavioral; otherwise drop |
| HowFar server | 007 config lift | `4d5726d` | `a78f899`, `e4233e1`, `ad4bda6`, `f5f65a1`, `cb9c523` | 5 rounds |
| HowFar server | 009 selfhost runtime | `6cfc0de` | `f41fd89`, `58d8e88`, `924ca69`, `be0950d`, `1084257` | 5 rounds |
| HowFar server | 013 region cross-check | `a45bd3e` | folded? | single commit — if fixes are folded in, use as bait or drop |
| HowFar server/docs | 014 selfhost merge | `368fe16` | `2a23405`, `d6bdc5b`, `6208fe6` | assertive docs mix |
| HowFar perf | 015 mobile perf audit | `0c4e4e5` | `11341d6`, `d573731` | 2 Critical (SwiftShader confound, n=1); strip the lockfile |
| HowFar UI | 017 perf defer map | `4bb6923` | `6963f1a` | |
| HowFar UI | 018 transit resilience | `5198fcc` | folded? | verify |
| HowFar UI | 020/022 preset reach | `6f6484a` / `2a15fcc` | `2a15fcc` / `1197a69` | |
| HowFar docs | 012 tile attribution | `fea1d0c` | `d58364d` | |
| HowFar bait | 016 phone-first design, 005 stale-claims docs | — | — | panels with zero accepted findings |

Target 16, minimum 12: ≥5 plugin enforcement, ≥4 HowFar server, ≥3 HowFar UI/client, 2 docs/assertive, 2 clean bait; ≥2 cases whose panel caught a Critical (036, 032, 015 qualify); ≥2 with zero accepted findings. Exclude: no recoverable reviewed commit, diff under 10 lines, tail-cert-only closes, and any case that cannot be brought under the 90k-char budget without gutting the diff.

### 4.3 Per-case recipe
1. `map_rounds.py` → pick the round and its reviewed commit; record the mapping in `case.json.notes`.
2. `case_from_task.py` → case dir. Fix `leak_scan` hits in `spec.md` by hand (delete the offending sentence; note it).
3. Write `truth.json` from the round's triage in task.md (and judge.md for wording): one entry per ACCEPTED finding whose fix is in a LATER commit (check `git show <fix>` touches the named file/symbol); `severity` = the triage's or the judge's word (Critical/Important/Minor; map "Blocker"→Critical, "Minor/nit"→Minor); `historical_outcome` = accepted+fixed or accepted+parked; `known_rejects` from explicit REJECT lines, with `file`/`symbol` when the rejected claim named them. Findings the panel accepted but whose fix is ALREADY in the reviewed diff are not truth for this case — drop them and note why.
4. Manually audit Design Phase answers in `spec.md` for post-review edits (phrases like "revised per", "after the panel", "the judge", references to later rounds); remove and note.
5. Secret scan `diff.patch` (`grep -inE 'api[_-]?key|secret|passw|token=|BEGIN (RSA|OPENSSH)|AKIA'`); public repos, but verify.
6. `python3 bench/judgebench.py corpus validate` after each case; `corpus show <id>`.
7. Freeze: `corpus.json` version 1 committed BEFORE any candidate runs; later additions bump the version and never edit existing cases (bias guard).

### 4.4 Difficulty labels
`easy` = a Critical the panel found unanimously in one round; `medium` = Important findings, some disagreement; `hard` = later-round cases (residual defects after a fix round) and clean bait.

### 4.5 Commits
One commit per 3–4 cases plus one for the tools; verify green each; push; CI 4 lanes. Panel: one impl round on the finished corpus (assertive) — ask it to check three cases' truth against their sources; triage; cap at two rounds.

### 4.6 Known limits to disclose in the corpus README section
Spec reconstruction is an approximation of the pre-review task.md; Design Phase answers are audited by hand; the reviewed commit may differ from the exact dirty tree the historical panel saw (fixes were not in it — that is what matters); severity labels come from prose.

### 4.7 Acceptance (Task A)
- `corpus validate` → `corpus v1: N cases OK`, N ≥ 12, strata met.
- Every case renders under 90k chars (Task B's `--transport` report, or `case_from_task.py`'s size print).
- Every `truth.json` finding cites a file that exists at `repo_base_sha` and a fix commit that touches it (a small checker in `bench/tools/` proves this mechanically).
- `run --fake --cases all --candidates sol-med,sol-high --run-id dry1` completes with exit 0; `adjudicate dry1 --auto`; `report dry1`.
- No secret in any `diff.patch`; no `.agent/` outside this task's record written.

## 5. Task B — rehearsal and ergonomics (plan step 10)

`reversible`, red-first, `tests/test_judgebench_*.py`:
1. `corpus validate --transport`: per case, rendered prompt chars/bytes and which of {stdin (claude/codex), argv POSIX (grok), argv Windows} it fits, using `LiveRunner.preflight` with a stub adapter factory; nonzero exit when any case exceeds the smallest transport of the Test A/B seats.
2. `--spec-mode compact` on `run`: spec = Intent + Why + References + Work Plan only (drops Design Phase), recorded in the manifest and in the result lines; used identically for every candidate of a run. Use it only if a case cannot otherwise fit.
3. **Contamination scan**: `judgebench contamination <run-id> --history PATH…` compares each raw judge output with the case's historical `judge.md`/`judge-archive.md` (read from the workspaces, read-only) for shared word 12-grams; flags candidates above a threshold in `report` as `contaminated?` with the matched span. Stdlib only. Disclose in README that this detects quoting, not silent influence.
4. Rehearsal: `run --fake` over the real corpus × 4 fake candidates with a script mixing ok/malformed/timeout/dnf; adjudicate a few findings interactively; report; fix ergonomics found. Record the transcript in the task.
Acceptance: the plan's §24 smoke command works verbatim on the real corpus; `--transport` shows every case fits grok; verify 9/9; 4-lane CI.

## 6. Task C — live runs (harness plan §25)

Prerequisites: owner spend approval (§7); `codex --version`, `grok --version` on PATH; grok Build balance checked (it read 402 in August); Task A frozen; Task B's transport report green.
1. Smoke: `python3 bench/judgebench.py run --cases <one small case> --candidates sol-med --run-id smoke1 --live --source-repo HowFar=~/Documents/Workspace/HowFarAI-v2/HowFar` in the background (a judge can exceed the 600 s foreground cap). Check `result.jsonl` status/usage/timing, the raw file, the journal line, the manifest's sandbox mode.
2. Test A: `run --cases all --candidates sol-med,sol-high --run-id testA --live --source-repo …` in the background; `--resume` after any interruption; `adjudicate testA` (human loop; `m <id>` for equivalent wordings, `v` for valid-new); `report testA --md bench/runs/testA/report.md`; `contamination testA`.
3. Test B: same with `grok-med,grok-high`, `--run-id testB`.
4. Decision rule (agreed in the harness plan §25): the costlier config wins only if severity-weighted valid findings per 1,000 known output tokens exceeds the cheaper's; where tokens are `unknown` (expected for codex/grok), fall back to per-invocation parity and SAY SO in the report. Copy both `report.md` files into the task record.
Budget: ~16 × 2 invocations per test, up to 20 min each worst case; hours, resumable.

## 7. Owner decisions pending
1. Spend approval for the smoke (1 codex call), Test A (32 codex Sol calls), Test B (32 grok calls).
2. Contamination stance: accept that live judges can read the historical `.agent/tasks` on this machine, with Task B's scan as the detector and a disclosure in every report — or defer live runs until a bench-local read jail exists (a `provider/sandbox.py` change).
3. Merge of `bench/judge-harness` — not required to run anything.

## 8. Handoff rules for the implementing session
- Work in `~/Documents/Workspace/playbook-plugin-dev` under full playbook discipline; one task per letter (A, B, C); classify honestly (A assertive, B/C reversible); red-first; `python3 scripts/verify` green before every commit; push; read your own CI (a superseding push CANCELS the in-flight run — hold pushes while a Windows lane you need is still running); reviews in the background; judges per `.agent/models.json`; NO version bump, NO merge, NEVER `claude plugin` commands; `provider/` untouched.
- Never edit the outer task.md while a background panel runs (the tamper guard hashes it); nested-tree edits are invisible to it.
- `git commit -F <msgfile>` — backticks in `-m "…"` trigger shell substitution.
- Windows lessons already paid for: reconfigure stdio to UTF-8 in any new CLI; `OpenProcess` succeeds for exited processes (check `STILL_ACTIVE`); `fnmatch` is case-sensitive on POSIX.
- Panels found hardening items every round on the harness; cap A at two rounds, B at one, C needs none (it produces data, not code).
- The bench reads both workspaces READ-ONLY; it never writes into any `.agent/` except this workspace's own task record.

## 9. Risks
1. Mapping errors between rounds and commits → wrong truth. Mitigation: `map_rounds.py` table + the mechanical fix-touches-file checker + a panel sample.
2. Cases too big for grok → Test B hollow. Mitigation: size budget at build time; compact spec mode as fallback; report excluded cases honestly.
3. Historical contamination of live judges. Mitigation: detector + disclosure; owner decision on a read jail.
4. Provider quota surprises (grok 402). Mitigation: smoke first; `--resume`.
5. n≈16 decides only large effects — the reports print point estimates only.
