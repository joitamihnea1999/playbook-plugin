# 032 - P1 Fence Blind Blocked
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

> Set assertive: the code diff is git-revertable, but the task extends a **claim about the world** — the PB-TASK-BLOCKED guarantee statement + a bound fence-safety proof in docs/guarantee-ledger.json. That claim (and its instrument, the new test) is reviewed regardless of diff size, and a task-record-corruption fix earns an implementation review. Rollback: `git revert` the code + ledger commit fully undoes it (no persisted data touched).

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
Route set_task_blocked/resume_blocked_task section location through the fence-aware _iter_nonfenced helper so a fenced ## Blocked example in task.md cannot delete or mis-splice the real record; sweep siblings

## Why
P1 defect parked by the 1.5.39 release panel (task 030 Parked; two judges converged — codex-sol #1 / codex-terra #2). `tasks handoff`/`tasks blocked` → `set_task_blocked` locates the `## Blocked` heading fence-blind, so a task.md that quotes a fenced `## Blocked`/`## Handoff` example can have its real record deleted or mis-spliced — the task record (the execution trace) corrupts. Session C already made the handoff *writer* (`write_handoff`) fence-aware via `_iter_nonfenced`; this closes the remaining writers it calls. Priority of the three parked findings per the owner (M193).

## References
- [x] Context: node [6] core.py owns the `## Risk`/`## Status` parsing + atomic task.md writers. The fence-aware helper is `core._iter_nonfenced` (core.py:947); the receipt writer `upsert_task_section` uses the equivalent `_closed_fence_line_indices` (core.py:2129); `write_handoff` (core.py:2370, Session C) + `_extract_block_reason` (2432) are already fence-aware. Spec: task 030 Parked [P1]. Guarantee owner: PB-TASK-BLOCKED (docs/guarantee-ledger.json).
- Playbook: playbook/Fix
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.


## Design Phase

### Chat Log Research
- [x] Reviewed. M193/M197 are the ground truth: P1 is the priority parked finding; standing rules — red-first, verify green per commit, push + read own CI to four green lanes, no version bump, no merge, default judge codex:gpt-5.6-terra:high, grok 402 = infrastructure (not a failure). Capped report; do not merge. Earlier M188–M192/M194–M196 are 1.5.39-release/retro noise — unrelated, trimmed below to the two that matter.

### Fix Orientation
- [x] What exactly is broken or needs cleaning up? — `set_task_blocked` (core.py:2192; `line.strip()=="## Blocked"` at :2206, span ends on `line.startswith("## ")` at :2210) and `resume_blocked_task` (core.py:2221; :2230/:2232) locate `## Blocked` **fence-blind**. A task.md quoting a fenced `## Blocked` example → the writer treats the fenced heading as live, deletes from it to the next `## ` (often the fence-closer or a real later H2), stranding an unclosed fence and swallowing real sections. `cmd_handoff` (lifecycle.py:938) calls `set_task_blocked(...,"handoff")`, so `tasks handoff` inherits the bug even though `write_handoff` is fence-safe.
- [x] What does "fixed" look like? (specific grep, test, or behavior) — both writers select the heading via `core._iter_nonfenced` and end the span on the next **non-fenced** H2. A fenced-decoy `set_task_blocked`/`tasks handoff` leaves the fenced block byte-intact (fences balanced, real gates preserved) and writes exactly one live `## Blocked`; normal (no-fence) block/resume/handoff flows are byte-identical to pre-fix output; existing handoff/blocked tests stay green.
- [x] What adjacent code could this break? — (a) idempotent re-block must still drop ALL prior live `## Blocked` sections; (b) resume's `> Resumed <ts>` stamp must land in the same place for normal files (byte-identical control); (c) the status pair `_set_status`/`_extract_status` (core.py:2113/2033) is ALSO fence-blind but OUT OF SCOPE — a bash/awk twin in the stop-hook reads `## Status`, locked by parity test test_blocked_state.py:185, so fixing it is a cross-language enforcement change disproportionate to the single-line hazard → report+park. Full sweep in Work Plan.
- [x] Test strategy: point tests, or also property tests (`hypothesis`) if fixing a parser/formatter/transformation? — point regression tests (section locator, not a numeric transform → no hypothesis). RED-first: watch a fenced-`## Blocked` decoy corrupt on current code (pure `set_task_blocked` + e2e `tasks handoff`), green after fix. Negative controls: normal block + resume byte-identical; existing handoff/blocked suites unchanged.

## Work Plan

> Fix/Verify pairs. What could this break?

- [ ] RED: add regression tests to tests/test_blocked_state.py
- [ ] Fix: route `set_task_blocked` + `resume_blocked_task` section location through `core._iter_nonfenced` (add a small module-level `_drop_live_section`/span helper)
- [ ] Verify: new tests GREEN; full `cd playbook-plugin && python3 scripts/verify` PASS.
- [ ] Sweep: enumerate every task.md section-locating writer/reader; fix fence-blind writers or report each as already-safe with the proving line

 **WRITERS (mutate task.md by section — the corruption class):**

 **READERS on the blocked/handoff path (agree with the fixed writers):**

 **KNOWN fence-blind — REPORTED OUT OF SCOPE (not corruption of the blocked/handoff record; each needs its own task):**
 - **Status pair** `_set_status` (core.py:2113, `line.strip()=="## Status"`) + `_extract_status` (core.py:2033) + the reopen writer `lifecycle.py:559`: fence-blind, but a `## Status`-in-a-fence corruption is a single-line overwrite, AND there is a **bash/awk twin** in the stop-hook that reads `## Status`, locked to Python by parity test `test_blocked_state.py:185 test_duplicate_status_headings_hook_agrees_with_python`. Fixing the Python side alone would BREAK that parity; fixing both is a cross-language enforcement-path change disproportionate to the hazard. → **PARK** (see Parked).
 - **Readers, non-corrupting misread** (produce phantom items from a fenced example but never rewrite the file): `extract_parked_items` (core.py:1715, phantom parked bullets), `_extract_problem` (core.py:2048), retro.py `_extract_section`/`_extract_status` (retro.py:150/164, analysis of completed tasks only). → **PARK**.
 - `standing_gates` heading-dedup (core.py:867): fence-blind but operates on freshly template-generated content at task creation, before any user-authored fenced example can exist → not exploitable in practice; noted, no action.
- [ ] Ledger: extend PB-TASK-BLOCKED statement to include fence-safety + bind the new proof & negative control in docs/guarantee-ledger.json; re-run the ledger validator
- [ ] Side effects: anything else that changed? Adjacent code still works? (mirror sync check


## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

