# 036 - Tail Certification
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
Owner decision A (ratified 2026-08-27): after a quorum-PASS implementation panel at tree F0, if the closing tree differs ONLY in non-behavioral file-classes (*.md outside task records: docs/README/CHANGELOG/MIND_MAP*; tests/; .agent/ task-records), tasks work done may satisfy freshness via a TAIL CERTIFICATION — the default single judge reviews the delta diff + panel verdict and returns PASS/FAIL. Any code-path delta (incl. code comments; file-class only) still needs a fresh full panel. --stale-panel-ok stays. Update docs + PB-PANEL-FRESHNESS + record the owner decision.

## Why
Owner decision A (ratified 2026-08-27). Evidence: the last five assertive tasks
across two workspaces ran 5–10 impl-panel rounds; the late rounds were
docs/comment/test-only deltas, and every one closed via `--stale-panel-ok` with a
written justification — the escape has become the norm and it exhausts vendor
quotas. (S2/task 035 THIS batch is a live instance: it closed a docs+test tail via
`--stale-panel-ok` because this mechanism didn't exist yet.) Tail certification
replaces the rubber-stamp escape with a real, cheap check (default single judge on
the exact delta) for the docs/test tail, while PRESERVING assertive discipline: an
independent judge still reads every claim-bearing docs delta, and any code-path
delta still forces a fresh full panel. Safety-critical direction: a WRONG tail-cert
that lets code through unreviewed is a serious hole, so the delta computation and
classifier must FAIL SAFE (unknown/ambiguous → require the full).

## References
- [x] Context: `.claude/bin/tasks recall keyword1 keyword2` (locates matching nodes across MIND_MAP.md + overflow), then `tasks recall <N>` for each → paste relevant excerpts below → recalled [7] lifecycle close path, [8] review.py panel write, [6] core.py freshness, [24] PB-PANEL-FRESHNESS:
 - **[7] lifecycle.py** owns the close path. Freshness is computed at `lifecycle.py:313-368`: `_impl` = first impl round in judge.md; `_impl["tree_state"]` = F0 stamp; `_now_fp = tree_state_fingerprint(project)`; `_carries` = rounds[0] is impl+PASS + panel_required. `freshness_gate_decision(...)` (pure policy) BLOCKS assertive/irreversible/unclassified when `round_fp != now_fp`, unless `--force`/`--stale-panel-ok --reason`. **This is where the tail-cert step inserts** — before the block, when STALE.
 - **[6] core.py** — `tree_state_fingerprint` = sha256 over HEAD+porcelain+diff+untracked-content, OUTER repo + each `code_roots` nested repo; **`.agent/` is EXCLUDED** (so task-record edits never move the fp — meaning the tail-cert delta can only contain code / `*.md` / `tests/`, never `.agent/`). `freshness_gate_decision` is PURE (no I/O). `parse_judge_rounds` gives rounds newest-first.
 - **[8] review.py** — the panel arm computes the round's `Tree-state:` and calls `stack_judge_round`. **The panel snapshot descriptor (F0 commits per scope) must be recorded HERE**, at the same instant as the stamp, because panel-time commits can't be reconstructed later.
 - **[24] PB-PANEL-FRESHNESS** (guarantee-ledger.json:517) — statement + proofs in `tests/test_panel_freshness_gate.py` (`ClosePathMatrix`). Owner symbols: `tree_state_fingerprint`, `freshness_gate_decision`, `close_decision`. Must extend the statement + add tail-cert proofs.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.


## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Review the "Recent Chat" messages captured in References (auto-injected at `tasks work`). Remove unrelated ones. Pull key user quotes, constraints, and context into Intent/Why above. The user's actual words are the ground truth for Intent. → Trimmed M223–M232 (HowFar + codex-credit chatter). Flagged M232 (grok as main judge) + M231 (codex limit) as separate user requests handled outside this task. Intent/Why reflect owner decision A.

### Understand
- [x] Restate the request in my own words. What does the user actually want? → Add a THIRD outcome to the stale-panel freshness gate. Today: fresh→close, stale→block (assertive/irreversible/unclassified) unless --force/--stale-panel-ok. New: stale BUT the delta since the panel's F0 touches ONLY non-behavioral file-classes → run a TAIL CERTIFICATION (default single judge reviews the exact delta diff + the panel's verdict summary) → PASS satisfies freshness (recorded in the receipt), FAIL blocks. Any code-path delta still needs a fresh full panel. --stale-panel-ok stays.
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? → Solving exactly owner decision A. The temptation is to over-engineer the delta computation (content manifests, transactional snapshots). I resist: the file-class rule is mechanical/path-based (owner: "no parsing heuristics, file-class only"), and the delta enumeration must be simple + FAIL-SAFE, not clever.
- [x] What would "done" look like? How will we know the task succeeded? → (1) a docs-only post-panel delta on an assertive task certifies via the single judge instead of blocking; (2) NEGATIVE CONTROLS all hold: a code-path delta (incl. a code-comment-only edit — file-class, not content) still blocks/needs a full panel; a FAIL certification blocks; reversible closes unaffected; `--stale-panel-ok` still works; a missing/mismatched panel snapshot descriptor falls back to the block (fail-safe). (3) docs (close-gate/cli.md/CLAUDE.md template) + PB-PANEL-FRESHNESS statement+proofs updated; owner decision recorded.
- [x] What are you assuming about the existing code/architecture that you haven't verified? → Verified: `.agent/` is fingerprint-excluded (so the tail-cert delta never contains task-records — they can't make a panel stale). Assumption to TEST: the delta between F0 and F_final can be enumerated per-scope (outer + each code_root) as `git diff --name-only <F0_commit>..HEAD` ∪ working-tree changes — requires recording each scope's F0 HEAD at panel time (a "panel snapshot descriptor"), because a committed-since-panel change in a nested code_root is otherwise invisible to a working-tree diff. Assumption: the default single judge can be invoked from the close path (reuse review.py's single-judge machinery).
- [x] What is OUT of scope for this task? → The F4 concurrency lock and F5 machine-round-delimiter (parked in task 035). NOT changing the panel/`--stale-panel-ok`/`--force` semantics. NOT touching reversible-risk advisory behavior. NOT a new content-manifest subsystem (path-based delta only). NOT removing codex from the panel (separate user request).

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. → BUILD (a new close-path mechanism). Sequence: (1) `record_panel_snapshot` at panel-write time [review.py] → `.agent/tasks/<N>/.panel-snapshot.json` = {tree_fp, scopes:{"":{commit,dirty:[paths]}, "<code_root>":{commit,dirty:[...]}}}; (2) `classify_delta_paths` [core.py] file-class classifier; (3) `tail_cert_delta` [core.py] reads the descriptor, enumerates the F0→F_final path union per scope, returns (can_certify, paths); (4) close-path integration [lifecycle.py] runs the tail-cert judge on a non-behavioral-only delta; (5) docs + ledger + owner-decision record. CHECKPOINT after (3): the delta enumeration is the safety-critical piece — confirm it's a complete superset before wiring the judge.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). → **assertive** (set above). If WRONG, what shows it: a tail certification that PASSes a delta which actually changed a CODE path (or a claim) would let unreviewed code close — the instrument is the negative-control test suite (code-path delta still blocks; a code-comment-only edit still blocks by file-class; FAIL cert blocks; missing/mismatched descriptor falls back to block) plus the PB-PANEL-FRESHNESS executable proofs. The claim asserted about the world lives in docs + the guarantee statement ("a non-behavioral-only post-panel delta may close via a single-judge tail certification; any code delta still requires a fresh panel"). Not irreversible (no data migration; descriptor additive under `.agent/`; git-revert restores prior policy).

### Reflection Gates
- [x] Wrote task-specific check questions → (a) Does a docs-only edit after an assertive panel-PASS close via tail-cert (single-judge PASS) WITHOUT --stale-panel-ok? (b) Does adding a single space to a `.py` file (code-comment/whitespace, non-.md/non-tests path) after the panel still BLOCK (file-class, not content)? (c) Does a tail-cert FAIL block? (d) With NO `.panel-snapshot.json` (or a tree_fp mismatch), does it fall back to the stale block? (e) Does a reversible close still skip freshness entirely? (f) Does `--stale-panel-ok --reason` still close? Each requires evidence from the close path, not yes/no.
- [x] Test strategy → Two layers. (1) Pure unit tests on `classify_delta_paths` (a table: docs/*.md → non-behavioral; README/CHANGELOG/MIND_MAP* → non-behavioral; tests/** → non-behavioral; .agent/** task-records → non-behavioral; *.py, scripts/*, a bare top-level *.md that isn't README/CHANGELOG, an unknown ext → behavioral) — file-class only, no content parsing. (2) Subprocess close-path integration in test_panel_freshness_gate.py (the PB-PANEL-FRESHNESS home), driving a real task dir + judge.md + `.panel-snapshot.json`, stubbing the tail-cert judge verdict, asserting the 6 check-questions above. Judge invocation is stubbed in tests (like the existing panel tests) — no live model. Red-first each.
- [x] Before the riskiest step: what would make you stop and reconsider? → The riskiest step is the delta enumeration (step 3): if it can UNDER-report (miss a code path that changed since F0), tail-cert would silently certify unreviewed code. Stop-and-reconsider trigger: if I can't prove the enumeration is a complete SUPERSET of everything the fingerprint could see (committed-since-F0 ∪ current-working-tree-changes ∪ paths-dirty-at-F0, per scope), I do NOT ship tail-cert — I fall back to the block. Also: if invoking the single judge from the close path proves to require live-model plumbing that can't be stubbed in tests, reconsider (tests must be deterministic).
- [x] If judging quality before building: is the gap worth closing? → Yes — the gap (rubber-stamp `--stale-panel-ok` on every docs/test tail, vendor-quota burn) is real and owner-ratified; S2 this batch hit it live. The blind plan-review panel (next gate) will stress-test the design — especially the delta-enumeration completeness and the file-class boundary — before I build the safety gate.

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. → Likely growth point captured as a plan checkpoint: the in-close judge invocation must be testable WITHOUT a production "force-PASS" backdoor (a safety hazard) and WITHOUT a live model. Plan splits the pure decision (`tail_cert_gate_decision`, no I/O) from the judge spawn, so the decision + delta-enumeration + classifier are unit-tested deterministically, and the spawn is a thin wrapper. The plan-review panel will pressure-test this seam before I build.
- [x] Does the work plan include moments where you stop and question your approach — not just execute? → Yes: a plan-review panel before building; a checkpoint after the delta-enumeration step (prove complete-superset before wiring the judge); and the impl-review panel after.
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? → Yes — the Design section names every file, symbol, the descriptor schema, the delta-enumeration formula, the classifier table, the 6 check-questions, and the fail-safe fallbacks; a fresh agent could execute the Work Plan from here. (Written deliberately handoff-ready given the task's size.)
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) → 8 work gates W1–W8: classifier / descriptor / delta-enum (safety checkpoint) / pure gate decision / close integration / deterministic judge harness / docs+ledger+decision / verify+mindmap. Each names its files, red-first check, and fail-safe.

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.
>
> **Design amendments, per gate:** **W1** — the LITERAL owner file-class set only; `docs/*.json` (ledger) + root `CLAUDE.md` are BEHAVIORAL. **W2** — do NOT write a separate `.panel-snapshot.json`; embed the F0 descriptor `{commit, -z dirty, per-path content-hash}` INSIDE the impl round next to `**Tree-state:**`, derived from the SAME git call as the stamp. **W3** — rename-safe `--name-status -z -M` classifying BOTH endpoints; fail closed on missing/mismatched descriptor, scope-set change, empty-path-set-while-stale, or any git error; detect reverted/deleted F0 paths via content-hash. **W4** — add a `TAIL-CERT-PASS` value to `_freshness`/receipt. **W5** — after judge PASS, RECOMPUTE the fingerprint and compare-and-swap before closing. **W6** — a DEDICATED `run_tail_cert_judge` with a structured `TAIL-CERT: PASS|FAIL` terminal token, fail-closed parse; never reuse `cmd_single_review`, never stack judge.md. No production force-PASS backdoor anywhere.

- [ ] **W1 — file-class classifier (`classify_delta_paths`, core.py), red-first.** Pure function: given a list of repo-relative paths, split into (behavioral, non_behavioral). NON-behavioral = `*.md` under `docs/` OR basename `README*`/`CHANGELOG*`/`MIND_MAP*` (any dir); any path under a `tests/` segment; any path under `.agent/`. BEHAVIORAL = everything else (incl. `*.py`, `scripts/*`, a top-level `*.md` that isn't README/CHANGELOG/MIND_MAP, unknown extensions, AND code comments
- [ ] **W2 — panel snapshot descriptor (`record_panel_snapshot`, review.py + core.py helper), red-first.** At panel-write time (right where the round's `Tree-state:` stamp is computed), write `.agent/tasks/<N>/.panel-snapshot.json` = `{"tree_fp": <fp>, "scopes": {"": {"commit": <outer HEAD>, "dirty": [porcelain paths]}, "<code_root>": {...}}}`. Under `.agent/` so it never moves the fingerprint. Check: after a panel run the file exists, tree_fp equals the round's stamp, and each scope's commit/dirty match git.
- [ ] **W3 — delta enumeration (`tail_cert_delta`, core.py), red-first — SAFETY-CRITICAL CHECKPOINT.** Read the descriptor; if absent OR `tree_fp` != the newest impl round's stamp
- [ ] **W4 — pure gate decision (`tail_cert_gate_decision`, core.py), red-first.** Inputs: `stale`, `can_certify`, `behavioral_nonempty`, `cert_verdict` (`"PASS"|"FAIL"|None`). Returns `(allowed, receipt_clause)`. Only reachable when the existing freshness gate would otherwise BLOCK (assertive/irreversible/unclassified + stale + carries + not force/stale_ok). Rules: not can_certify OR behavioral_nonempty
- [ ] **W5 — close-path integration (lifecycle.py).** In the STALE branch, BEFORE the block: if `--force`/`--stale-panel-ok` not given and the gate would block, call `tail_cert_delta`; classify; if `can_certify` and no behavioral paths, spawn the DEFAULT SINGLE JUDGE (reuse the impl-review single-judge machinery) with a tail-cert prompt (the exact delta diff + the panel's verdict summary; instruct: the panel already PASSed the code at F0, confirm this non-behavioral delta does not invalidate that verdict
- [ ] **W6 — deterministic test harness for the in-close judge.** Add a fake judge provider (a tiny stub CLI the test puts on PATH / sets as `default_judge`) that emits a controllable PASS/FAIL, so the W5 subprocess integration tests are deterministic with no live model. Confirm the stub path cannot be reached in production by accident (it's only selected when the configured default_judge points at it).
- [ ] **W7 — docs + guarantee ledger + owner-decision record (assertive core).** Update: close-gate docs (architecture.md/the freshness section), `docs/cli.md` (`tasks work done` freshness paragraph), the CLAUDE.md **template** text describing freshness; extend PB-PANEL-FRESHNESS statement + add tail-cert proofs (PASS-certifies-nonbehavioral, code-delta-still-blocks, FAIL-blocks, missing-descriptor-falls-back) with negative controls; record owner decision A + rationale (where owner decisions live
- [ ] **W8 — full verify + mind map.** `python3 playbook-plugin/scripts/verify` 9/9 green; update MIND_MAP node [7] (close path) in place with the tail-cert third outcome. No provider mirror touched (review.py/core.py/lifecycle.py are canonical, not `provider/`)


## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

