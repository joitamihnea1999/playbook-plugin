# 015 - Mobile Perf Audit
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

> **Why assertive, not reversible:** the deliverable is a set of measured claims about the world (perf numbers + pass/fail vs budgets) that becomes the next task's work plan — a claim about the world is `assertive` regardless of how small the diff is (rule: an instrument/number is a claim too, rule 13). The committed measurement scripts themselves are reversible code, but the numbers they publish are not. Impl review is therefore required, focused on the CLAIMS and the INSTRUMENTS (were the numbers taken on the prod build? median-of-N? cold/warm separated? emulation flagged?). No `src/` behavior change; the app tree stays byte-identical (bundle-analyzer is run out-of-tree, see Structure note).

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
Measure mobile performance of the map flow against owner budgets (Lighthouse mobile, bundle, runtime profile, API latency); no optimization changes — produce a written gap list that becomes the next task's work plan; measurement scripts must re-run on a real Android via one command.

## Why
Mobile is the product's headline surface (node [36]: the dark map-first shell "*is* the product's headline deliverable"), and the commercial pivot ([0](9)) makes real-device performance a launch gate, not a nicety. The owner has set hard budgets (TTI ≤2.5s mid-range/4G, pan/zoom ≥55fps with no frame >32ms, initial JS ≤350KB gz incl. MapLibre, API p95 150/300/800/400ms, Lighthouse mobile ≥90). Before spending effort optimizing we need honest numbers and a ranked gap list. This task is **measurement only** — the fixes are the NEXT task. Getting the numbers wrong "in the reassuring direction" is the classic failure (rule 13); every headline number must be taken on the shipped production build, not a dev server or a prototype.

## References
- [x] Context: recalled nodes [36][6][21][5][23]; self-host stack state probed. — see excerpts below
- Playbook: playbook/Investigate
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.

### Recent Chat (auto-captured at activation — review and remove unrelated)

**[M057]** [2026-08-29 06:06:30]
Work under playbook discipline on HowFar (nested HowFar/ repo, branch main). H2 — the MOBILE PERFORMANCE AUDIT: a measurement task, NO optimization changes (numbers first; the fixes are the next task)...

### Context excerpts (from mind map + environment probe)
- **[6] AppMap & controllers** — `AppMap.tsx` is a thin client shell; imperative MapLibre/network glue is split into `create*()→{…,dispose}` controllers (`camera`, `hover`, `ring-reveal`, `route-path`, `popup`, `amenities`, `select-flow`, `reach-*`). The owner's ask #3 ("gesture paths render-free") maps directly here: pan/zoom should be MapLibre-internal, NOT triggering React re-renders. This is the controller-architecture claim to verify empirically.
- **[36] Responsive shell** — dark map-first shell is the headline deliverable; `shell-state.ts` decides layout, `long-press.ts` handles touch gestures, transitions are reduced-motion aware. Guarded by a Pixel real-touch Playwright project + `ui-mobile.spec.ts`.
- **[21] Stack** — Next.js 16.2.10 App Router, React 19.2.7, Tailwind 4, MapLibre GL 5, `pmtiles` basemap. Node 24.x.
- **[5] Quality/CI** — e2e serves the PRODUCTION build (`next build`); rebuild + `fuser -k 3799/tcp` before Playwright (rule 7 / negative-knowledge 7). Playwright port is 3799.
- **[23] Self-host** — providers via `docker/selfhost` overlay; app pointed by env only (`npm run dev:selfhost` brings up db + engines and runs `npm run dev`). For a PROD build I must serve `next start` with the same overlay env, not `dev`.
- **Environment probe (2026-08-29):** Docker 29.6.1 present; self-host volumes (`nominatim-data`, `ors-graphs`, `ors-elevation`, `nominatim-flatnode`) + artifacts (`data/selfhost/photon/`, `data/osm/romania-260824.osm.pbf`, `data/tiles/selfhost-romania.pmtiles`) all EXIST. `google-chrome` installed; Playwright Chromium 1228 cached. Lighthouse NOT installed (npx needs network+consent). Node v24.11.0.
- **Transit caveat:** `/api/transit` + `/api/reach` are NOT self-hosted (MOTIS/GTFS gate) — they hit the public network even under the overlay. API-latency measurement covers suggest/geocode/isochrone/car/amenities (self-hosted); transit is out of the local-stack latency scope and noted as such.

---

## Design Phase

### Chat Log Research
- [x] Review the "Recent Chat" messages captured in References (auto-injected at `tasks work`). Remove unrelated ones. Pull key user quotes, constraints, and context into Intent/Why above. The user's actual words are the ground truth for Intent. — reviewed [M057]: single request, fully on-topic. Six deliverables + budgets pulled into Intent/Why + Work Plan; owner constraints in Scope guardrails; nothing unrelated to remove.

### Investigation Orientation
- [x] What's the question or hypothesis? State it before looking. — What are HowFar's real mobile-map-flow perf numbers on the PROD build, and where does each fall vs owner budgets? Deliverable = ranked gap list (measured · pass/fail · cause · fix · effort) = next task's work plan.
- [x] What evidence would change your mind? — a budget already met → no fix row; a number swinging cold/warm or run-to-run → report a range not a point; if pan/zoom DOES cause React re-renders the controller-architecture claim [6] is falsified → headline finding.
- [x] When do you stop? (convergence criteria: N rounds with no new position, or specific answer found) — all 6 deliverables produced with reproducible numbers, each budget has a gap row, real-Android re-run scripts checked in + dry-run-verified. No optimization (out of scope).
- [x] Test strategy: if findings lead to code changes, point tests or also property tests (`hypothesis`) for invariants? — the measurement scripts ARE the claims (rule 13): each hand-validated on one real run before quoting; Lighthouse/trace = median-of-N (N≥3); cold vs warm separated; every emulation number tagged `[EMU]`; scripts parameterized by URL+throttle so `--device real` re-runs via adb one command.

## Work Plan

> Rounds: build the instrument → validate it on one real run → take the number (median-of-N) → checkpoint. Stop when all 6 deliverables have reproducible numbers + a gap row.

### Round 0: Measurement harness (infra — the instrument)
- **Goal:** production build served locally with the self-host provider stack + reusable, re-runnable measurement scripts checked into the repo (`scripts/perf/`), parameterized by target URL + throttle profile + `--device emulated|real` so a real Android re-run is ONE command (adb reverse + Chrome remote debugging on `localhost:9222`).
- **Steps:** (a) `next build`; (b) bring up self-host stack (`dev:selfhost:down`-safe; reuse existing volumes) and serve `next start` on a fixed port with the provider overlay env; (c) seed the amenity catalogue is already imported? verify `/api/ready` 200 + `/api/catalogue-status` available; (d) author `scripts/perf/README.md` + the runner scripts.
- [ ] Checkpoint: prod build serving, self-host `/api/ready` green, harness scripts hand-validated on one real run each.

### Round 1: Lighthouse mobile (deliverable 1)
- **Hypothesis:** MapLibre + basemap on the critical path pushes TTI/TBT over budget; LCP/CLS likely OK (map canvas paints fast, dock is static).
- **Test:** Lighthouse mobile preset (Moto G-class CPU 4×, 4G throttle) against the main map flow URL; capture Performance score, TTI, LCP, TBT, CLS; median of N≥3.
- **Result:** median of 5 (all 5 runs near-identical): **Perf score 67** (budget ≥90 → FAIL), **TTI 7723 ms** (budget ≤2500 → FAIL, ~3× over), **LCP 2368 ms**, **TBT 2386 ms** (very high — main-thread blocked by 470KB gz JS parse/exec), **CLS 0.098** (just under 0.1 "good"), FCP 904 ms (good), Speed Index 2349 ms. Hypothesis confirmed: TTI/TBT blown, LCP borderline, CLS OK. All `[EMU]` (Lighthouse simulated Moto-G/Slow-4G).
- [ ] Checkpoint: numbers stable across runs? report point or range.

### Round 2: Bundle analysis (deliverable 2)
- **Hypothesis:** initial JS gz > 350KB driven by MapLibre GL (~200KB+ gz alone); app + vendors add the rest; pmtiles/turf/d3-contour may or may not be on the critical path.
- **Test:** production `.next` build output → initial/first-load JS gz, broken down MapLibre vs app vs vendors; what's on the critical path vs lazy/dynamic-imported; largest modules. Use `@next/bundle-analyzer` run OUT OF TREE (env flag, no committed config change) + raw `.next/*` gz sizes as the ground-truth cross-check.
- **Result:** Method note: Next 16 uses **Turbopack** (no per-route sizes printed, no `app-build-manifest.json`), so ground truth = the JS the browser actually pulls over the wire on initial load (`transferSize` == gzipped bytes, cache disabled), cross-checked against `gzipSync` of each on-disk chunk. **Initial (critical-path) JS = 470.7 KB gz** (raw 1704.7 KB) → **budget ≤350 KB FAIL (+34%)**. Breakdown: **MapLibre+pmtiles 327.1 KB gz** (one 1.20 MB-raw chunk — alone ≈ the whole budget), **react-dom 69.6 KB**, **app+vendor 74 KB**. **Lazy-loaded: 0 KB** — nothing is deferred; MapLibre is statically imported (`selection-render.ts` value-imports `maplibre-gl`; `AppMap` is a static import in `page.tsx`, no `next/dynamic`) so it loads on first paint. turf/d3-contour are **absent** from the client bundle (isochrone geometry is server-only in `server/` modules) — good. `results/bundle.json`.
- [ ] Checkpoint: breakdown reconciles with the raw first-load number Next prints?

### Round 3: Runtime profile of the 3 hot interactions (deliverable 3)
- **Hypothesis (owner's ask to verify):** the controller architecture [6] keeps pan/zoom gesture paths render-free (MapLibre-internal, no React re-render). Address-select→ring-reveal and mode-toggle DO re-render (expected) but should be bounded.
- **Test:** Chrome trace + React profiling for: (i) address select → ring reveal, (ii) mode toggle, (iii) pan/zoom. Capture main-thread long tasks, dropped frames / frame times, and React commit count during each. Instrument re-renders (React DevTools profiler API / `<Profiler>` onRender count captured via injected script — measurement only, not committed to app).
- **Result:** React commits counted via an injected `__REACT_DEVTOOLS_GLOBAL_HOOK__` (prod React still fires `onCommitFiberRoot`), with an equal-length IDLE control. **A (address select → ring reveal):** 17 commits, 517 ms main-thread long-task, ~3.4 s wall. **B (mode toggle walk→car):** 3 commits, 3018 ms long-task (heavy — car ring render + amenity re-placement). **IDLE baseline:** 60 fps, 0 commits, 0 ms long-task. **C (pan/zoom):** **median 20 fps** (budget ≥55 → FAIL), **worst frame 83 ms** (budget ≤32 → FAIL), 44/84 frames >32 ms, **0 React commits**. Key contrast: idle 60 fps vs active-gesture 20 fps, both ~0 commits ⇒ the frame drops are MapLibre's own scene repaint (rings + 8,774 amenity markers) under the 4× CPU throttle, **not** React. All `[EMU]` — the 4× throttle dominates the fps number; a real device (no throttle) will be materially better and MUST be re-measured before treating 20 fps as the shipping number. `results/runtime-profile.json`.
- [ ] Checkpoint: is the gesture path actually render-free? (falsifiable

### Round 4: API latency from the browser (deliverable 4)
- **Hypothesis:** self-hosted suggest/geocode/isochrone/car/amenities are fast warm (cache hit) but cold cost is dominated by ORS isochrone + amenities PostGIS intersect.
- **Test:** from the browser (Resource Timing / fetch timing), measure suggest / geocode / isochrone / car / amenities against the LOCAL stack, cold (first call, cache cold) and warm (repeat). Report p50/p95 per endpoint. Transit/reach EXCLUDED (not self-hosted — noted).
- **Result:** browser fetch, unthrottled, 12 samples/cell, cold = fresh ApiCache key (real varied Bucharest inputs) vs warm = repeat (cache hit). Budget on **cold p95** (first-touch). **suggest** cold p50/p95 52/**393** ms (budget 150 → **FAIL**, tail spikes; p50 fine), **geocode** 42/149 (300 → PASS), **reverse** 25/48 (300 → PASS), **isochrone** 132/183 (800 → PASS), **car** 133/188 (800 → PASS), **amenities** p50/p95 795/**865** ms (400 → **FAIL**, 2.2×; PostGIS intersect over 8,774 places). **Warm all 2–6 ms** (ApiCache hits — trivially pass). Caveat: unthrottled/local numbers; real-device 4G adds ~50–150 ms RTT per call on top (pushes suggest/geocode/reverse toward their budgets). `results/api-latency.json`.
- [ ] Checkpoint: cold vs warm clearly separated; p95 computed from enough samples.

### Round 5: Synthesis — THE GAP LIST (deliverables 5 + 6)
- [ ] Build the gap list: one row per owner budget
- [ ] Tag every emulation-based number `[EMU]` and state exactly what needs real-Android re-measurement + the one command to do it. Confirm scripts are checked in + `--device real` path documented and dry-run-verified.
- [ ] What remains unknown / follow-up: what the FIX task should investigate that measurement couldn't settle.

---

## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

