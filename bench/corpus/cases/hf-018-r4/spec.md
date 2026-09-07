# 018 - H Fix 1 Transit Resilience Hover Lan
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

> Set at Structure gate. Code changes (retry, timeout, hover guard, next.config, gitignore) are `reversible` — `git revert` fully undoes them, no persisted/world state. But item 3 documents the LAN-testing procedure + stale-SW gotcha in `docs/SELFHOST.md` = a **claim about the world** → the whole task is `assertive` (heaviest wins). Instrument that would reveal the doc-claim false: following the documented LAN steps on a real device either works or it doesn't — verified by actually loading the app over LAN (browser live-verify) and confirming the SW-clear step resolves the foreign-route 404s. Needs an impl review to close (also required by `panel_required_for=all`).

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
H-fix-1 (owner live testing): (1) transit-family resilience — one server-side retry w/ short backoff on fetch-failure/abort across /api/transit + /api/reach + amenities-in-transit cascade, modestly longer plan-endpoint timeout, honest degraded copy; (2) guard hover-controller pickAmenity queryRenderedFeatures against amenity-source data-swap race (feature index out of bounds); (3) formalize next.config allowedDevOrigins + document LAN testing & stale-SW gotcha in docs/SELFHOST.md; (4) gitignore test-results/ in workspace. Red-first on 1&2.

## Why
Four defects surfaced in the owner's live testing on a real phone over LAN. They are all trust hits (owner priority #1): the transit family fails intermittently in a way an identical retry heals (so the product reads as flaky, not broken), a real console error spams from the hover controller, and LAN phone testing has an undocumented setup + a stale-service-worker trap that costs the owner debugging time. None are new features — they harden what already ships.

## References
- [x] Context: recalled the owning mind-map nodes. **[10] Transit Reachability** — `server/transit.ts` one-to-all → `transit-grid.ts` contours; `transit-plan.ts` handles the directions plan; cache key `transit:v5:`. **[15] Right-Click Directions (Reach)** — `reach-directions-controller.ts` owns `/api/reach` fetch with generation/abort/**12s deadline** (`REACH_TIMEOUT_MS`), renders into `ReachPanel.tsx` dock. **[18] Provider Client Template** / **[24] Caching** — `lib/provider-http.ts` wraps every upstream call (`providerFetch` = per-`${provider}@${host}` rate bucket + `timedFetch` AbortController). **[23/36] Self-Host + Responsive** — `docs/SELFHOST.md` is the self-host runbook; long-press = right-click on mobile.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.

### Key code facts (verified by reading source this task)
- `transit-plan.ts`: `PLAN_BUDGET_MS = 10_000` absolute abort budget; per-attempt `timeoutMs = PLAN_BUDGET_MS`; ALREADY has an **empty-response** retry (gated `Date.now()+RETRY_MIN_REMAINING_MS(3000) < deadline`) but NOT a **fetch-failure/abort** retry. `fetchPlanBody` wraps every raw error into `ProviderError` before it escapes. Test `transit-plan.test.ts` asserts `PLAN_BUDGET_MS < REACH_TIMEOUT_MS`.
- `transit.ts` (one-to-all): `TIMEOUT_MS = 20_000`, NO retry — a single fetch failure → `ProviderError` → 502. This is also the amenities-in-transit cascade path (`catalogue.ts` calls `transitIsochrone`).
- Honest degraded copy ALREADY exists: reach → "The routing service is slow — please try again." (`reach-directions-controller.ts`); transit mode → `failureMessage` "Could not compute public-transport reach. Try again." (`selection-flow.ts`); amenities → "Amenities unavailable right now" (`AmenityPanel.tsx`). So item 1's copy deliverable is mostly satisfied; verify + lightly tune, do not over-scope.
- `hover-controller.ts`: `pickAmenitiesAt` (click path, task 061) ALREADY wraps `queryRenderedFeatures` in try/catch; `pickAmenity` (hover path) does NOT — that is the "feature index out of bounds" race source.
- `next.config.ts`: currently NO `allowedDevOrigins`. Next 16 has it as a **top-level** key (promoted from `experimental` in 15.2).
- Outer workspace `.gitignore`: no `test-results/` entry; `?? test-results/` is untracked (a `.last-run.json`).

### Recent Chat (auto-captured at activation — review and remove unrelated)

**[M079]** [2026-09-02 06:10:28]
The codex seats have recovered (owner live-verified a real generation just now; grok may still 402 — infrastructure). Resume task 018 with tasks work 018 and close it properly: run tasks compact first...

---

## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Reviewed auto-captured chat. Removed **M067** (unrelated — it belongs to task 016 phone-first design). **M068** IS this task; its four items + the standing constraints (nested `HowFar/` repo, branch main, **never push — owner pushes**; judges per `.agent/models.json`; reviews in background; tail-certification, plugin 1.5.41; red-first on items 1&2) are captured in Intent/Why above.

### Understand
- [x] Restate the request in my own words. What does the user actually want? — Four independent owner-observed hardening fixes: (1) transit family (`/api/transit`, `/api/reach`, amenities-in-transit) survives transient upstream flakiness via ONE retry on fetch-failure/abort + short backoff + a modestly longer plan-endpoint timeout, honest copy when it still fails; (2) stop `pickAmenity → queryRenderedFeatures` crashing when the amenity source is mid-swap; (3) make the manual `allowedDevOrigins` edit proper + documented, + write down LAN-testing & the stale-SW 404 trap; (4) gitignore the workspace's untracked `test-results/`.
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? — The temptation is to over-engineer item 1. The delicate part is the plan budget invariant (`PLAN_BUDGET_MS < REACH_TIMEOUT_MS`, the heal-loop killer). I will REUSE the existing budget/deadline machinery, not redesign it, and only touch the transit family the owner named (no "retries everywhere").
- [x] What would "done" look like? How will we know the task succeeded? — Red-first: a flaky-provider fake (fail-then-succeed) proves each retry heals (one-to-all + plan); a deterministic source-swap fake proves `pickAmenity` no longer throws. Plan gets one modestly-longer-timeout knob still `< REACH_TIMEOUT_MS`. `next.config.ts` reads dev origins from env; `docs/SELFHOST.md` gains a followable LAN section. Workspace `git status` no longer shows `?? test-results/`. check:ci + e2e green; plan+impl panels at quorum; browser live-verify where feasible.
- [x] What are you assuming about the existing code/architecture that you haven't verified? — (a) 10.0s abort == `PLAN_BUDGET_MS` (verified = 10_000). (b) `ProviderError` wraps raw errors, so retry MUST classify BEFORE wrapping → retry sits at the `providerFetch` call site (verified reading `fetchPlanBody`/`fetchAndBuild`). (c) Raising the plan budget forces raising `REACH_TIMEOUT_MS` to keep the asserted `<` invariant — will confirm the test passes with new numbers.
- [x] What is OUT of scope for this task? — Client-side retries; retries on non-transit providers (Nominatim/Photon/ORS/car); MOTIS self-host/GTFS; UI redesign; making the rate-limiter abort-aware (accepted residual, already documented in code); pushing anything (owner pushes). Item 4 = OUTER workspace repo only.

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. — **Build** (four small independent fixes). Sequence low→high risk so a red is cheap early: **item 4** (gitignore, workspace) → **item 2** (hover guard, red-first) → **item 3** (next.config env + docs) → **item 1** (transit retry/timeout, red-first, delicate, LAST). Items are verification-independent (separate files) → no split needed (rule 11); item 1's plan-budget change + its test are one seam. Checkpoint after item 1's red-first tests before touching production code.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small. — **assertive** (see `## Risk`). If WRONG: the doc claim (LAN steps + SW-clear fixes the 404s) is falsifiable by following it on a real device over LAN; the retry claim is falsifiable by the flaky-provider fake (fail-then-succeed must heal; a persistent failure must still surface honest copy, not hang). Instrument = red-first tests + browser live-verify. No rollback plan needed (reversible code); assertive purely because of the docs.

### Reflection Gates
- [x] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" — the answer should require evidence, not just yes/no) — (Q1) Does a fetch that fails ONCE then succeeds produce a SUCCESSFUL result via exactly 2 provider calls? (Q2) Does a REAL upstream status (`!res.ok`, e.g. 429) make exactly 1 call (NOT retried)? (Q3) Does a persistent transient failure still throw `ProviderError` (→ honest 502 copy) and cache nothing? (Q4) Does `PLAN_BUDGET_MS < REACH_TIMEOUT_MS` STILL hold with the new numbers? (Q5) Does `pickAmenity` return `null` (not throw) when `queryRenderedFeatures` throws "out of bounds"? (Q6) With no env set, does `next.config.ts` still yield `allowedDevOrigins: undefined`?
- [x] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic) — Point tests (Vitest), red-first: (1) `transit.test.ts` + `transit-plan.test.ts` flaky-provider fakes (transient throw on call 1, resolve call 2 → assert success + call count) + a "!res.ok status → NOT retried" test; reuse the fake-timer budget harness for the plan retry. (2) hover: reuse the `dispose-contract.test.ts` fake-map + `createLoadState()` pattern (styleLoaded=true, `queryRenderedFeatures` throws) → `pickAmenity` returns null; add as `hover-controller.test.ts`. (3) a pure unit for `isRetriableFetchError`. No property tests warranted.
- [x] Before the riskiest step: what would make you stop and reconsider? — Riskiest = raising `PLAN_BUDGET_MS`/`REACH_TIMEOUT_MS`. STOP if the `<` invariant test can't stay green without a large client bump (>~15s = bad UX), or if the flaky fake shows the existing empty-response retry double-counting with the new failure retry. Fallback: retry-only-on-FAST-failure, no budget bump, slow-remote stays honest-copy.
- [x] If judging quality before building: is the gap worth closing? — N/A build task; the "retry vs longer timeout" question was judged in the critique: BOTH, because they heal different modes (network drop vs slow remote) and the owner named both.

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. — Added two growth points at the end of the Work Plan (retry re-entering the rate bucket; fake-timer backoff handling). Also noted the amenities cascade inherits transit.ts's retry (no separate gate).
- [x] Does the work plan include moments where you stop and question your approach — not just execute? — Yes: W8 carries the explicit STOP condition from the Reflection gate (revert to retry-only-on-fast-failure if the budget invariant can't hold or calls double-count). The Structure gate sets a checkpoint after item 1's red-first tests before production code.
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? — Yes: each gate names the file, the exact red-first assertion, and the class-grep. The delicate invariant (`PLAN_BUDGET_MS < REACH_TIMEOUT_MS`) and the ProviderError-wrapping constraint (retry must sit at the `providerFetch` call site) are spelled out in References + W6-W8.
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) — 11 gates across 4 items, red/green split where it matters. Under the ~15-gate split threshold; items are file-independent so no verification-seam split needed (rule 11).

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

**Item 4 — gitignore (workspace repo, lowest risk, do first)**
- [ ] W1: Add `/test-results/` to the OUTER workspace `.gitignore`; confirm `git status` at workspace root no longer lists `?? test-results/`. (Risk: none

**Item 2 — hover + sibling pick race guards (red-first; ACCEPT-C, rule 5 — 3 sites)**
- [ ] W2: RED
- [ ] W3: GREEN

**Item 3 — LAN dev origins (hostname-only) + docs (ACCEPT-D/F/G)**
- [ ] W4: `src/lib/dev-origins.ts`
- [ ] W5: docs/SELFHOST.md

**Item 1 — transit-family resilience (red-first, delicate — LAST; ACCEPT-A/B/E/H)**
- M068 signatures pinned: (1) `/plan` *"operation aborted at 10.0 s"* = SLOW-but-working remote → cure is the modest budget RAISE (longer single attempt); (2) *"fetch failed" 502* = FAST network drop → cure is the remaining-budget-gated retry. One-to-all shares signature (2); its heavy 20 s stall is signature (1) with no client deadline, so it needs an absolute budget.
- [ ] W6: RED
- [ ] W7: one-to-all (`transit.ts`)
- [ ] W8: `/plan` (`transit-plan.ts`)
- [ ] W9: client-retry interaction + honest copy (ACCEPT-B decision).

**Cross-cutting close-out**
- [ ] W10: Rebuild `.next` + `fuser -k 3799/tcp`, run full `check:ci` (unit+coverage+lint+budgets) + e2e green. Record counts.
- [ ] W11: audit sweeps recorded + MIND_MAP updated + mindmap_check clean.
  - **S1 stale-claim** (`grep -riE "12s deadline|fetch at 10s|client's 12"`): found 5 stale timing comments I'd left after moving `REACH_TIMEOUT_MS 12→14`/`PLAN_BUDGET_MS 10→11` — fixed the CLASS by rephrasing them constant-relative (`PLAN_BUDGET_MS`/`REACH_TIMEOUT_MS`, no magic seconds) in `transit-plan.ts` (×3) + `reach-directions-controller.ts` (×1) + the earlier doc line; re-grep clean (only the intentional "12s→14s" change-note remains); tsc clean; 117/117 on the 4 touched test files after.
  - **Publication hygiene** (`hygiene_check.py --repo HowFar` over staged diff, 19 files): **PASS**.
  - **Test-count**: `test_count_guard.py` EXIT 0; the increase is intentional (~26 added tests across the 4 items) and all green in check:ci's 1164-test run — rule 2 satisfied (movement up, explained).
  - **S3 tautology/mock-echo**: reviewed the new tests — all assert against REAL function output (`retryOnceOnTransient`/`isRetriableFetchError`/`transitIsochrone`/`planTrip` with `vi.fn` inputs), no `expect(X).toBe(X)`, no hand-built mock echoed back.
  - **MIND_MAP**: updated owning nodes in place — **[18]** (provider transient-retry plumbing: `isRetriableFetchError`/`retryOnceOnTransient`/`ProviderError.retriable`, one-to-all `ONE_TO_ALL_BUDGET_MS`, `/plan` unified 2-call loop + `PLAN_BUDGET_MS 11 < REACH_TIMEOUT_MS 14 ≤15`) and **[6]** (shared `safeQueryRenderedFeatures` hit-test guard across the 4 sites). `mindmap_check.py` clean (0 phantom / 0 undated / 0 retired-no-marker). `tasks audit` PASS (its 2 findings — stale-markers 1077, mindmap-stale-refs 1 — are pre-existing, identical to the pre-work audit, not mine).

**Growth points noted:** (a) a plan retry re-enters the shared-host rate bucket (adds `intervals.transit` spacing) — bounded by the budget signal, acceptable; document. (b) backoff `setTimeout` under fake-timer tests needs `advanceTimersByTimeAsync`; keep backoff small (~250 ms) so real-timer tests stay fast.

---

## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

