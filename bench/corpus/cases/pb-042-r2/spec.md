# 042 - Review Spend Journal
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

> Log-only best-effort code (the record write can NEVER change a review or a decision — same hard contract as the enforcement journal), so the runtime behavior is reversible. But this task is **assertive** because it (a) extends the guarantee ledger (`docs/guarantee-ledger.{json,md}`) — a machine-checked claim about the world — and (b) documents the record shape in the journal contract docs for playbook-lens to consume. Instrument that would reveal the claim false: the red-first tests (`tests/test_review_spend_journal.py`) + the guarantee-ledger validator (`scripts/guarantee_ledger.py`, run in verify) which resolves every owner/proof reference. Panel impl-review required (per `panel_required_for` + this Risk).

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
Best-effort review-spend records: every judge invocation (seat, single, tail-cert) appends one hook=review decision=record journal line via pb_journal — seat, task, round, kind, wall duration, exit status, token usage (unknown when CLI does not report it)

## Why
The owner wants to monitor Playbook's token/review spend so it doesn't burn tokens uselessly (M293: "we should also monitor the usage of tokens for playbook … we don't have infinite tokens"). Reviews (seats especially) are the dominant spend surface. Today nothing records what each judge invocation cost — no seat, duration, or usage trail — so there is no data to optimize against. This adds the observability layer (same class as the enforcement journal) that a later playbook-lens can consume to attribute and reduce spend. Owner-ratified 2026-09-01.

## References
- [x] Context: recalled nodes [8] review.py, [5] enforcement journal, [24] verify/ledger. Read `scripts/pb_journal.py` (contract: never-raise, one O_APPEND under PIPE_BUF, lane-only), `lifecycle.py:515-559` (the existing `decision="record"` close emitter — the pattern to mirror), `review.py` panel arm `run_judge`+as_completed loop (~904-982), single-judge `cmd_single_review` common completion (~2010) + `_bail_review_timeout` (timeout exit), tail-cert `_run_tail_cert_judge_raw` (~1363). Ledger: `docs/guarantee-ledger.json` PB-ENFORCEMENT-JOURNAL + `scripts/guarantee_ledger.py` resolves every owner/proof reference (runs in verify). Tests: `tests/test_enforcement_journal.py`.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.


## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Trimmed the chat to M293 (motivation: monitor/optimize token spend) + M295 (the owner-ratified spec) and pulled both into Why/Intent; dropped M291/M292/M294 (rate-limit notices + unrelated h-fix design).

### Understand
- [x] Restate the request in my own words. What does the user actually want? — every judge invocation leaves one best-effort spend line in the enforcement journal (hook="review", decision="record"): seat, task, round, kind (|single|tail-cert), wall duration, exit status (ok|fail|timeout|dnf), token usage where the CLI reports it (never fabricated). Emitted where the subprocess already finished; a write failure never affects the review.
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? — solving the stated one (spend observability). NOT building the reader/analytics (lens = separate repo), NOT changing judge output format to harvest usage (would ripple through the whole review pipeline). Staying in the log-only lane the owner named.
- [x] What would "done" look like? How will we know the task succeeded? — red-first tests prove a record lands per panel seat + per single judge + tail-cert, an unwritable journal changes nothing, format pinned; ledger extended + validator green; contract docs describe the record shape with the honest usage skew; verify green 4 lanes; impl panel PASS.
- [x] What are you assuming about the existing code/architecture that you haven't verified? — verified: pb_journal.append writes exactly the enforcement envelope; claude `-p` judge runs PLAIN TEXT (no `--output-format json`) so usage is not surfaced today → `unknown` in practice unless a real usage JSON shape appears (read headless_argv/run_headless_judge/format_judge_output). That skew is documented, not a defect.
- [x] What is OUT of scope for this task? — a journal reader/CLI/dashboard; switching judge output to json for usage; codex/grok usage numbers (spec: duration + unknown marker); any behavior change to reviews/gates/closes; playbook-lens itself.

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. — build + assertive tail. Sequence: (1) pb_journal.append_review + _write_record [done]; (2) review.py emit helper + usage/round helpers + wire 3 emit sites; (3) red-first tests; (4) ledger sibling + contract docs + mind-map. Under 15 gates.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small. — `assertive`. Instrument: guarantee-ledger validator (in verify) resolving every owner/proof reference + pinned-format test + per-seat/per-single record tests. Runtime git-revertable (log-only); assertive class forces the impl panel.

### Reflection Gates
- [x] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" — the answer should require evidence, not just yes/no) — (1) Does a panel emit exactly one `hook=review,decision=record` line per seat, all with the same `round`? (2) Does a single judge emit exactly one, with `kind=single` and a `duration_ms`? (3) With the journal dir made a regular FILE, does the review still return its verdict byte-for-byte (write failure changes nothing)? (4) Is the pinned record shape (keys + envelope) exactly as documented? (5) Is `usage` `{"status":"unknown"}` when no usage line is present, and `{"status":"known",...}` when a real usage JSON shape is?
- [x] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic) — point tests in `tests/test_review_spend_journal.py`: unit-level `append_review` (format pinned, unwritable-lane no-op, byte-cap), plus in-process `_journal_review_spend`/`_judge_status`/`_parse_judge_usage`/`_next_review_round` against a temp lane; and a panel-path test that monkeypatches `run_headless_judge` to a fast fake and asserts N seat records land. No hypothesis needed (no arithmetic/parser invariants beyond the tiny usage regex, which gets explicit +/- cases). stdlib unittest.
- [x] Before the riskiest step: what would make you stop and reconsider? — if wiring the emit changed a review's control flow or return value (it must be purely additive/best-effort). Risk sites: the panel `run_judge` tuple arity change (unpacked in exactly one loop + nowhere else — verified) and the tail-cert signature change (one caller — verified). Stop if the existing suite regresses.
- [x] If judging quality before building: is the gap worth closing? — n/a (building, not pre-judging an existing artifact).

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. — growth point captured: the record shape must be documented for playbook-lens (separate repo) with the honest usage skew; added as a work gate. Also confirmed pb_journal.py / review.py have NO mirror copies (mirror is only `scripts/lib/provider/`) so no rsync.
- [x] Does the work plan include moments where you stop and question your approach — not just execute? — yes: after wiring the emits I ran in-process smoke tests (pb_journal + review helpers) BEFORE writing the test file, to confirm the additive-only design holds; the plan-review panel is the next reassessment point.
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? — yes: the record shape, emit sites (3), and contract are all documented inline + in the ledger/contract doc.
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) — 6 work gates (pb_journal, review wiring, tests, ledger, contract doc, mind-map) + review/pre-review. Right size.

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

- [ ] **W1 — pb_journal.append_review + _write_record.** Add a sibling `append_review(agent_dir, *, session_id, seat, task, round_no, kind, duration_ms, status, usage)` producing the enforcement envelope (`hook="review"`, `decision="record"`, `reason="review spend"`) + review fields; factor the O_APPEND write into `_write_record` (shared with `append`, byte-identical). Contract preserved: never raises, one write under PIPE_BUF, lane-only. Check: existing `test_enforcement_journal` still green; append_review smoke shows 2 lines, right envelope.
- [ ] **W2 — review.py emit wiring (3 sites).** Module helpers (`_load_pb_journal` cached, `_journal_review_spend`, `_judge_status`, `_parse_judge_usage`, `_next_review_round`) + emit at: panel per-seat (as_completed loop, shared round), single-judge common completion + `_bail_review_timeout` (timeout), tail-cert `_run_tail_cert_judge_raw` (all return paths). Purely additive/best-effort. Check: full suite green (no control-flow regression); in-process smoke shows a record per emit.
- [ ] **W3 — red-first tests (`tests/test_review_spend_journal.py`).** Prove: a record lands per panel seat + per single judge (+ tail-cert); an unwritable journal changes the review's outcome NOT AT ALL (negative control); format/envelope pinned; usage unknown-vs-known; byte-cap. Watch each new assertion FAIL against a stubbed-out emitter first (red), then pass.
- [ ] **W4 — guarantee ledger.** Add sibling `PB-REVIEW-SPEND-JOURNAL` (owner/proof references that the validator resolves: `symbol:append_review`, the new test methods) + note the owner decision; update `guarantee-ledger.md` if it enumerates. Check: `scripts/guarantee_ledger.py --summary` + `test_guarantee_ledger` green.
- [ ] **W5 — record-shape contract doc.** Document the record shape for playbook-lens (a docs file), with the HONEST usage skew (claude judge = plain-text mode
- [ ] **W6 — MIND_MAP.md.** Update the owning node ([8] review + [5]/[24] journal) in place to record the review-spend record surface; no new node.

---

## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

