# 039 - Fence Discipline Consolidation
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

<!-- CONFIRMED assertive (structure gate, this session): this CHANGES the risk
close-gate's behavior (uncertain fence parse → BLOCK) and TIGHTENS the
PB-RISK-CLASSIFY / PB-TASK-BLOCKED ledger claims — a claim about the world +
its instrument. Needs a full impl-panel to close. V7 (the `## Status` bash/awk
lockstep) is SPLIT OUT to its own task, so this task no longer touches the
enforcement parity twin; scope is core.py (V1-V6), Python-only. -->


> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
One strict shared task.md fence scanner with per-consumer fail directions (readers fail-open, destructive writers fail-closed, risk close-gate uncertain→block) + indented-code-block awareness + NBSP/tab in fence closers & ATX headings + fenced-## Status stop-gate; the P-A..P-F residuals from tasks 032/033

## Why
The task.md fence scanners are on the ENFORCEMENT path — they decide whether `## Risk`, `## Status`, `## Blocked`/`## Handoff`, and receipts are seen as live. Six pre-existing best-effort bounds (P-A..P-F from tasks 032/033) let an adversarially-malformed committed task.md defeat a gate: an unclosed fence above `## Risk` forces a LENIENT legacy close (P-C, codex-sol #1 Critical); an NBSP fence closer or an indented (≥4-col) code block exposes a decoy classification (P-A/P-B); a fenced `## Status` example can release unchecked gates (P-F). Today these are contained only by human diff review + the OS sandbox. This task converges the scanners to ONE strict engine with a per-consumer fail direction and makes the six bounds robust — tightening the PB-RISK-CLASSIFY / PB-TASK-BLOCKED claims. Delayed = the gates stay defeatable by crafted markdown.

## References
- [x] Context: the authoritative spec lives in **task 032 Parked** (`.agent/tasks/032-p1-fence-blind-blocked/task.md`, the `[fence-consolidation …]` + `[P1-followup]` + `[status-pair]` + `[readers]` bullets) and **task 033**, plus the ledger limitations on **PB-RISK-CLASSIFY**, **PB-TASK-BLOCKED**, **PB-CLOSE-VERIFY-CONTRACT** (`docs/guarantee-ledger.json`). Re-read those first — they hold the exact vectors, the sweep table, and the disclosed bounds. Owning node: **[6] tasks core (`core.py`)** for the scanners; enforcement parity twin is `scripts/gate-echo-lib.sh` (bash/awk `## Status` reader) + `scripts/stop-hook`.

**Code map (verified this session):**
- `core.py::_iter_nonfenced` (~L936) — the strict CommonMark scanner for section-locating WRITERS/readers; fails CLOSED (an UNCLOSED opener fences to EOF) because its consumers DELETE/replace sections.
- `core.py::_closed_fence_line_indices` (~L2129, moved) — the twin for the receipt writer; fails OPEN (unclosed remainder is live) because it only INSERTS.
- `core.py::_risk_heading_lines` (L917) → uses `_iter_nonfenced`; `has_risk_section` (L1024) / `extract_risk` (L1001) consume it. **The Critical bypass:** an unclosed fence above `## Risk` makes `_iter_nonfenced` hide the heading → `has_risk_section`→False → close_decision treats it as pre-1.5.0 legacy → LENIENT close. The risk-gate needs the OPPOSITE fail direction from the delete-writers (uncertain parse → `risk_section_present=True` → BLOCK).
- `_live_section_span` (L980) + `write_handoff._section_span` — H2 boundary via `startswith('## ')` on the indent-stripped line (misses `##\tX`, over-matches `>=4-space ## X`).
- Fence regex `_FENCE_OPEN_RE` + `_RISK_HEADING_RE`/`_RISK_FIELD_RE` (L911-914).
- Readers that misread-but-don't-corrupt: `extract_parked_items`, `_extract_problem`, `retro._extract_section`/`_extract_status`; block/handoff readers `_extract_block_reason`, `find_unconsumed_handoff`, `_latest_receipt_line`.

**Controls already in place** (do NOT weaken; extend): `test_provider_multiuser.py`, `test_blocked_state.py` (incl. `BlockedEndToEnd.test_duplicate_status_headings_hook_agrees_with_python` — the bash/awk↔python parity), the fence/handoff/receipt suites, `test_risk_*`. All 6 vectors are PRE-EXISTING on origin/main (verified in task 032), best-effort bounds today.
- Playbook: playbook/Fix
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.


## Design Phase

### Chat Log Research
- [x] Reviewed. M321-M329 were all from an unrelated project (howfar/transit) — removed. M330 is this task's activating prompt; distilled its binding constraints into the Recent Chat note, Intent (already set), and Why (filled in). Ground-truth Intent unchanged from the spec.

### Fix Orientation
- [x] What exactly is broken or needs cleaning up? — TWO hand-rolled scanners (`_iter_nonfenced` L936 fail-CLOSED, `_closed_fence_line_indices` L2839 fail-OPEN) share opener/closer rules but are two impls, and BOTH miss (a) NBSP fence closers (`.strip()` eats U+00A0 → P-A), (b) indented (≥4-col) code blocks (P-B). Bypasses: P-C — unclosed fence above `## Risk` hides the heading → `has_risk_section`=False (L1052) → `close_decision` (L2265) treats as pre-1.5.0 LEGACY → lenient close. ATX boundary `_live_section_span`/`write_handoff._section_span` use `s.startswith("## ")` → miss `##\tX` (P-D). Readers `_extract_block_reason`/`_latest_receipt_line`/`find_unconsumed_handoff` are fail-CLOSED → an unclosed fence above a LEGIT `## Blocked`/`## Handoff`/receipt HIDES it. V7/P-F: `_extract_status`/`_set_status` (L2743/L2823) + awk `scripts/stop-hook:118` pick the LAST `## Status` fence-blind.
- [x] What does "fixed" look like? (specific grep, test, or behavior) — ONE strict CommonMark scanner (opener ≤3-space `` ``` ``/`~~~`, backtick-info-no-backtick rule, closer same-char ≥len + ASCII-whitespace-only, indented-code-block tracking) exposing `unclosed_is_live: bool`; delete-writers pass False (fail-closed), receipt-writer + pure readers pass True; `has_risk_section` returns True on an uncertain parse (unclosed fence) → close BLOCKS. Red-first decoy per vector closes red then green; the 92 control tests stay green; new NBSP/indented/unclosed decoys no longer defeat the risk gate.
- [x] What adjacent code could this break? — every scanner consumer: blocked/handoff WRITERS (stay fail-closed), receipt writer/audit reader (fail-open — never refuse an insert), risk/status gates. Parity test `test_blocked_state.py::BlockedEndToEnd.test_duplicate_status_headings_hook_agrees_with_python` locks Python↔awk on `## Status` → V7 is lockstep cross-language. Work in `core.py`; NO `provider/` change → no rsync.
- [x] Test strategy: point tests, or also property tests (`hypothesis`) if fixing a parser/formatter/transformation? — point + end-to-end decoy tests per vector (red-first); existing parity/negative tables are the regression controls. `hypothesis` is NOT available (stdlib-only, hard constraint) → exhaustive hand-built decoy tables (NBSP, tab, ≥4-space, unclosed, nested-fence, `##\t`) as the property substitute. **Decision: SPLIT V7** (fenced `## Status` stop-gate) to its own follow-up task — the only cross-language lockstep vector; spec+handoff both flag "split if heavy"; keeps V1-V6 a coherent Python-only consolidation. V7 spec captured in Parked.

## Work Plan

> Fix/Verify pairs. What could this break?

> **Approach (from task 032 Parked, distilled).** ONE strict CommonMark scanner
> used by every task.md section locator, exposing a per-consumer FAIL DIRECTION
> (an argument like `unclosed_is_live: bool`), so the two hand-rolled scanners
> (`_iter_nonfenced` fail-closed, `_closed_fence_line_indices` fail-open) become
> one implementation with two call-time policies. Do it RED-FIRST per vector;
> the existing parity/negative tables (test_provider_multiuser, test_blocked_state,
> the fence/handoff/receipt suites) are the controls — run the FULL suite after
> each change (behavior change on unclosed fences must be DELIBERATE). Land the
> vectors as separate Fix/Verify pairs (add gates as you go):

- [ ] V1
- [ ] V2
- [ ] V3
- [ ] V4
- [ ] V5
- [ ] V6
- [ ] V7
- [ ] Verify (each vector)
- [ ] Side effects


## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

