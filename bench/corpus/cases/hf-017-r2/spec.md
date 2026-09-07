# 017 - Perf Defer Map And Amenities
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

**Why assertive, not reversible:** the task's deliverables include a PERF_AUDIT before/after table (measured performance numbers) and a README section stating what each CI budget means and enforces — both are *claims about the world*, void the moment the shipped code's real numbers disagree with them. The code changes (dynamic-import boundary, PostGIS tweaks, warmup) are individually `git revert`-able, but the published measurements are not.

**Instrument that would reveal the claims false (rule 13 — re-take on the SHIPPED code):** `scripts/perf` re-run on the prod build + self-host stack — `analyze-bundle.mjs` (build-manifest + wire bytes), `run-lighthouse.mjs` (TTI/score, emulated), `run-api-latency.mjs` (amenities cold p95, n≥30). If the before/after table or the README budget claims do not match a fresh run of these on the merged code, the claim is void. The CI gate (`perf:budget` size check + Lighthouse-CI) is itself a standing instrument: it must actually FAIL on an injected regression (mutation-proof), or the "budgets block closes" claim is documentation not enforcement (rule 13, 001 corollary: an instrument nobody runs / that can't fail reports nothing).

**No irreversible step:** no data migration, no secret, no publish/push (owner pushes). The self-host DB is read-only for measurement (EXPLAIN ANALYZE is read-only; any index added is a forward migration with a documented drop).

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
H3a: defer MapLibre off first-load critical path (lazy shell + dynamic map), cut amenities cold p95 (warm-path cache + PostGIS + readiness warmup), enforce initial-JS + Lighthouse-mobile budgets in check:ci, re-run perf instruments and update PERF_AUDIT with before/after

## Why
Task 015 measured the mobile map flow against the owner budgets and 6/8 FAIL (`docs/PERF_AUDIT.md`). The map is the portfolio headline (brief §3) yet TTI is ~3× budget (7.7 s vs 2.5 s), initial JS is +34% (470.7 vs 350 KB gz — MapLibre 327 KB gz eagerly on the critical path), Lighthouse mobile 67 (<90), and amenities cold p95 905 ms (>2.3× the 400 ms budget). H3a closes the **design-independent** gaps (H4 owns UX/visual redesign; gap #4 pan/zoom fps stays OPEN pending the owner's real GPU device — do NOT chase it under software WebGL). Without enforced budgets, any future change silently regresses these again — so the fixes must ship WITH CI gates that block a regression from closing.

## References
- [x] Context — recalled nodes [6] AppMap/controllers, [13] amenity runtime query, [24] caching, [26] health/ready, [29][5] CI, [36] responsive shell + the 015 perf observation, [19] status contract. Key facts: **[6]** `AppMap.tsx` is a thin client shell (1453 lines) statically importing `maplibre-gl` + `pmtiles` at module top; imperative glue is in `create*()` controllers; `page.tsx` (server) statically imports `AppMap` → MapLibre is in the route's first-load JS. **[13]** amenities: `catalogue.ts` orchestrates → `resolveClip` (walk/car = arithmetic, transit = MOTIS call) → 24h result cache (`amenity:local:v5:`, keyed dataset+clipIdentity+coords, single-flight) → miss: `clipRingsFor` (ORS ~185 ms, ApiCache-backed) → `queryCatalogueSummaryInRing` PostGIS intersect over 8,774 places (`ST_Intersects` against outer ring, `ST_Intersection`+`ST_PointOnSurface`, NTILE stratification). Warm path already 6 ms; cold-cold 905 ms = ORS ~185 + PostGIS ~720. **[24]** ApiCache is Postgres-backed, no reaper (parked). **[26]** `/api/ready` probes DB+PostGIS+migrations+config+region; `lib/health.ts` holds the probe. **[36]** map surface IS the headline deliverable; 015 audit is a dated observation. Self-host stack volumes (nominatim/ors/photon) + built artifacts (`data/selfhost/` 3 GB) + app dev DB with imported catalogue ALL still present → `npm run dev:selfhost` (day-2, no re-import) can bring the stack up to re-measure.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.


## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Review the "Recent Chat" messages captured in References (auto-injected at `tasks work`). Remove unrelated ones. Pull key user quotes, constraints, and context into Intent/Why above. The user's actual words are the ground truth for Intent. — M059 IS this task; captured into Intent/Why. M060 (H4a) removed. Binding quotes: "node [0] binding, no visual redesign (H4 owns UX)", "never push — the owner pushes", "Gap #4 (fps) stays open pending the owner's real device; do not chase it under software WebGL", "ENFORCE the budgets in CI ... so regressions block closes", "numbers, not adjectives", "reviews in the background", "Stop after close with the before/after table."

### Understand
- [x] Restate the request in my own words. What does the user actually want? — Close design-independent PERF_AUDIT gaps: (1) MapLibre+non-essential vendors off first-load (initial JS ≤350 KB gz, TTI ≤2.5 s emulated) via paint-fast shell + dynamic map import; (2) amenities cold p95 905→≤400 ms via warm-path ring caching + PostGIS EXPLAIN/optimization + readiness warmup (gaps #5/#6 ride along); (3) ENFORCE both budgets in `check:ci` + README docs (assertive); (4) re-run `scripts/perf`, update PERF_AUDIT with measured before/after.
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? — Risk = a satisfying deep AppMap shell/canvas split or chasing pan/zoom fps. Guardrails: gap #4 explicitly OUT (owner: no software-WebGL chase); map-defer = LOWEST-risk boundary that hits the budget, not a refactor of the headline surface; PostGIS work is EXPLAIN-first, not speculative index tuning.
- [x] What would "done" look like? How will we know the task succeeded? — Re-measured on the SAME instruments/host as 015: build-manifest initial-route JS ≤350 KB gz with MapLibre lazy; TTI ≤2.5 s + Lighthouse mobile ≥90 emulated; amenities cold p95 ≤400 ms (or measured+attributed+honest if the PostGIS floor can't reach it); `check:ci` fails on either budget regression; PERF_AUDIT before/after table; full check:ci + e2e green; impl panel at quorum.
- [x] What are you assuming about the existing code/architecture that you haven't verified? — (a) `next/dynamic(ssr:false)` on AppMap drops MapLibre from route build-manifest first-load (verify `.next/app-build-manifest.json`); (b) no other initial-graph module statically re-imports maplibre-gl; (c) cold cost is PostGIS-dominated (verify with spans + EXPLAIN ANALYZE, don't assume); (d) the imported volumes/catalogue reproduce ~905 ms (re-run the api instrument to establish MY baseline first — rule 13).
- [x] What is OUT of scope for this task? — pan/zoom fps (gap #4, owner-deferred to real device); transit/reach latency (GTFS gate); ApiCache reaper (parked); visual/UX redesign (H4); any push/deploy (owner pushes); 2nd-city/region (P4).

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. — COMBINATION, measure→build→measure. 3 workstreams + a mid-point checkpoint (rule 11, >15 gates): WS-A client map-defer (build+measure), WS-B amenities cold path (investigate-first: spans+EXPLAIN, THEN build), WS-C CI budget enforcement + README (build, assertive). Baseline re-measure FIRST (my own numbers before any change — rule 13); CHECKPOINT before the final re-measure+doc.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small. — Set **assertive** (see ## Risk block). Falsifier = `scripts/perf` re-run on the shipped code + a mutation-proof that the CI budget gate actually fails on an injected regression. No irreversible step.

### Reflection Gates
- [x] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" — the answer should require evidence, not just yes/no) — (1) Does `.next/app-build-manifest.json` for `/` list ZERO chunk whose gz contains a maplibre/pmtiles signature, and is the summed gz of the route's first-load chunks ≤350 KB? (2) Does the browser still fetch the maplibre chunk (map works) but only AFTER the shell is interactive (lazy, not initial)? (3) Do all 17 Playwright specs (incl. mobile Pixel) still pass? (4) Do server-side spans show the ORS-vs-PostGIS split, and does EXPLAIN ANALYZE show the GiST index used on `AmenityPlace.geom`? (5) After WS-B, is amenities cold p95 (n≥30, fresh keys) lower than my baseline, by how many ms? (6) Does `npm run check:ci` exit non-zero when I inject a +100 KB import into the initial bundle (mutation-proof)?
- [x] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic) — Point tests: a `perf:budget` node check parsing `app-build-manifest.json`, bucketing chunks, asserting the initial-JS ceiling AND no maplibre signature in the initial set (unit-testable against a fixture manifest + mutation-proof by injection). Amenities contract tests stay green against real PostGIS via existing integration tests (rule 4). Readiness-warmup: unit test that warmup calls each provider once and never throws into readiness. EXPLAIN ANALYZE is a recorded measurement, not a test. Playwright is the regression net for the defer refactor. No property tests warranted (no new parser/arithmetic).
- [x] Before the riskiest step: what would make you stop and reconsider? — Riskiest = the AppMap defer (headline surface). STOP if: any Playwright spec regresses without an obvious fix; the dynamic boundary does NOT remove maplibre from build-manifest first-load (approach invalid → reassess); or TTI stays >2.5 s with maplibre lazy (shell itself heavy → different fix). Second riskiest = a PostGIS index/DDL: STOP before any migration if EXPLAIN shows the index already used (cost is elsewhere → report the real floor, don't add a useless index).
- [x] If judging quality before building: is the gap worth closing? — Yes, already measured (015): TTI 3× over, initial JS +34%, Lighthouse 67, amenities 2.3× over, on the headline surface. Map-defer is high-leverage (one boundary fixes #1/#2/#3+LCP). The amenities floor may be partly irreducible (PostGIS over 8.7k geoms); the honest deliverable there is a measured, attributed result, not a guaranteed pass.

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. — Growth points already seeded as explicit gates: WS-A's "who statically re-imports maplibre-gl" reassessment; WS-B's fork on whether the floor is irreducible; WS-C's server-dependency decision for Lighthouse-CI; WS-A's selection-continuity decision-by-measurement. Likely additional growth: the defer may require extracting a small maplibre-free shell if AppMap's search can't hydrate fast enough — captured in the WS-A selection-continuity gate.
- [x] Does the work plan include moments where you stop and question your approach — not just execute? — Yes: WS-0 baseline gate (reproduce before fixing), WS-A boundary-verification gate (reassess if maplibre stays in first-load), WS-B EXPLAIN gate (don't add an index if already used), and the explicit CHECKPOINT before WS-D. Each has an evidence-bearing **Check**.
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? — Yes: WS-0→D is a measure→build→measure sequence with the instrument commands named (`scripts/perf`, `dev:selfhost`), the boundary approach spelled out, the falsifying instruments in ## Risk, and OUT-of-scope fenced. A fresh agent could reproduce the baseline and follow the checks.
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) — Right granularity: one gate per verification seam (a boundary rebuild, an EXPLAIN, a re-measure), not per line edited. ~19 work gates across 4 workstreams + checkpoint — over the 15-gate heuristic, but it's ONE measure-fix-remeasure cycle the owner scoped as H3a with a single before/after close, so the mid-point CHECKPOINT (not a task split) is the right control (rule 11 counterweight: splitting would create cross-task measurement dependencies).

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.


### WS-0 — MY baseline (DONE before the panel; measure before touching anything; rule 13)
- [ ] Bring up the self-host stack (`docker/selfhost` up
- [ ] `next build` (prod) + serve; run `scripts/perf` bundle + lighthouse + api-latency on CURRENT `main`
- [ ] **(F8) Make the paired before/after causally comparable:** freeze a committed deterministic sample set (seed the cold-coord jitter / fix the origin list) so "before" and "after" exercise the SAME polygons; document the ApiCache-flush protocol (regenerable cache, not a data migration). **Check:** two baseline runs on unchanged code agree within noise on the SAME inputs.

### WS-I — Fix the measurement instrument FIRST (prereq for WS-A Check + WS-C gate; F1)
- [ ] Replace `analyze-bundle.mjs`'s `networkidle0`=initial heuristic with an EXPLICIT lifecycle classifier ..
- [ ] Validate the fixed instrument against one real build by hand (rule 13

### WS-A — Map-free shell + deferred map engine (gaps #1/#2/#3; F1+F2)
- [ ] **(F2) Real boundary, not whole-AppMap defer:** ... **Check:** the eager shell renders header+search with ZERO maplibre in its import graph.
- [ ] **(F1) Trigger policy — implement the owner's stated "map hydrates behind it" (auto-load after shell interactive), instrumented by the WS-I mark.** ...
- [ ] Audit every eager vendor in the shell's initial graph (turf, d3, pmtiles, prisma leakage)
- [ ] **(F2) Selection continuity across the boundary:** ... **Check (new e2e):** an address submitted before the map loads results in rings drawn once it loads.
- [ ] Rebuild + `fuser -k 3000/tcp` (prod, not dev) + full Playwright (incl
- [ ] Re-measure bundle + lighthouse ..

### WS-B — Amenities cold p95 (gap #8; #5/#6 ride along; F3+F4+F5)
- [ ] Add server-side spans to `nearbyAmenities`/`computeNearbyAmenities` ..
- [ ] `EXPLAIN (ANALYZE, BUFFERS)` the intersect query on the live 8,774-place dataset ..
- [ ] Apply the smallest EXPLAIN-justified optimization (..
- [ ] **(F4) Verify the ORS single-flight, don't re-architect:** ... **Check (integration test):** one selection ⇒ one ORS computation.
- [ ] **(F3) Keep the cold-cold probe as the VERDICT instrument ...** Add any in-session ... NEW separately-labelled cell ...
- [ ] **(F5) Warmup done SAFELY, and only if the cause is warmth:** ... **Check:** concurrent `/api/ready` probes don't duplicate warmup nor re-hit providers; a failed provider neither marks done-forever nor floods; readiness latency unaffected.

- [ ] **(discovered — the REAL amenities fix) Apply `MATERIALIZED` to the `intersections` + `clipped_rows` CTEs** in `catalogue-query.ts`. ...

### WS-C — Enforce the budgets in CI + document (gap #3; assertive; F6+F7)
- [ ] Build the deterministic bundle/laziness gate ..
- [ ] **(F7)** Lighthouse-mobile budget ... **(F6)** Runs as a SEPARATE explicitly-invoked job (`perf:lighthouse`) whose recorded result is cited at close
- [ ] **(F6) Make the gate actually run in CI:** ... **Check:** the gate is a real CI step ...
- [ ] Document in README what each budget means + enforces (assertive) ..

### CHECKPOINT (rule 11 — reassess before the final re-measure)
- [ ] Pause: WS-I/A/B/C all green ...?

### WS-D — Re-measure the full suite + update PERF_AUDIT (assertive close-out)
- [ ] Final `next build` + full `scripts/perf` re-run on the MERGED/shipped code ..
- [ ] Update `docs/PERF_AUDIT.md` with a before/after table ..
- [ ] Update MIND_MAP node [36] (and [24]/[13]/[26] if their contracts moved) in place ..


## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md

