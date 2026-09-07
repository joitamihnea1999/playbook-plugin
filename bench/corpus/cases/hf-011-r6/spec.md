# 011 - P3 Selfhost Provider Stack
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
Self-host the provider stack LOCAL ONLY: docker-compose for Nominatim + Photon + ORS (foot-walking + driving-car) + a local pmtiles pipeline, all built from ONE Geofabrik Romania extract, wired to the app purely through the P1/P2 env config. Parity-check geocoding + walk/car rings against the public providers at the 2026-07-24 audit origins with stated tolerance and measured numbers. Default public config stays byte-identical and fully green with no local stack running. Transit/MOTIS OUT (GTFS licence gate), no prod/Railway/paid-infra change, data under a gitignored path.

## Why
The commercial pivot (MIND_MAP [0] ruling 9) is to run the whole OSM stack self-hosted so HowFar costs only a flat server + domain — no per-call/per-user API fees, and the "not for commercial" community APIs (Nominatim/Photon public, ORS public free tier) become unusable once money is involved. Task 007 (P1) made every provider host + the extent config-driven, byte-identically defaulting to today's public Bucharest values; task 009 (P2) made the provider RUNTIME self-host-correct (per-provider rate buckets, keyless self-host ORS, `PROVIDER_DATA_REVISION` cache namespace). Everything is in place *except the engines themselves*. **This task (owner calls it P3) actually stands the engines up locally** — Nominatim + Photon + ORS + a local pmtiles pipeline from ONE Geofabrik Romania extract — and proves, with measured parity numbers, that the app pointed at them via `.env` returns the same answers the public providers do. What breaks if delayed: the pivot stays theoretical (config knobs with nothing behind them), and there is no evidence the self-hosted target is correct before it would ever be deployed.

**Owner scope (M045, authoritative):** IN — compose stack for Nominatim + Photon + ORS (foot-walking + driving-car) + pmtiles pipeline, all from ONE Romania extract, wired purely through env; ops documented with *measured* disk/RAM/import-time (not guessed); data under a gitignored path; parity-check geocoding + walk/car rings vs the public providers at the 2026-07-24 audit origins with stated tolerance + measured numbers ("never 'looks the same'"); default public config stays byte-identical + fully green (check:ci + e2e) with NO local stack running. OUT — transit/MOTIS self-hosting (BLOCKED on the owner's Bucharest GTFS licence check — transit keeps its current provider untouched); any prod/Railway change; any paid infra; weather/air-quality; multi-city UI; renaming Bucharest literals (parked P4). Never push without asking.

## References
- [x] Context: recalled nodes [0] (rulings), [18] (provider client template), [23] (self-hosted basemap), [8]/[9] (search/walk), [25] (geofence). Key excerpts below.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.

**App contracts the self-hosted engines MUST serve (verified from source, branch `self-host`):**
- Nominatim (`src/features/search/server/nominatim.ts`): `${NOMINATIM_BASE_URL}/search?format=jsonv2&countrycodes=ro&viewbox=<bbox>&bounded=1&limit=1&q=…` and `${…}/reverse?format=jsonv2&lat=&lon=`. Default host `https://nominatim.openstreetmap.org`.
- Photon (`src/features/search/server/photon.ts`): `${PHOTON_BASE_URL}/api?q=&bbox=<bbox>&lat=44.43&lon=26.10&limit=8&lang=en`. Default `https://photon.komoot.io`.
- ORS (`src/features/isochrones/server/ors.ts`): POST `${ORS_BASE_URL}/v2/isochrones/{foot-walking|driving-car}`. Default `https://api.openrouteservice.org`; keyless when base host ≠ public host (task 009). NB self-hosted ORS docker serves under `/ors` → operator sets `ORS_BASE_URL=http://host:8080/ors`.
- Tiles (`src/app/api/tiles/route.ts` + `scripts/fetch-tiles.sh`): serves `TILES_PMTILES_PATH` (default `data/tiles/bucharest.pmtiles`) with HTTP Range. Today the pmtiles is CUT from the public `build.protomaps.com` planet build; P3 goal per acceptance-bar #1 is to BUILD it locally from the Romania extract.
- Extent: `NEXT_PUBLIC_MAP_BBOX="25.8,44.2,26.4,44.7"` (minLng,minLat,maxLng,maxLat) = geofence + maxBounds + tile `--bbox`, one source. BUILD-TIME inlined.

**Parity provenance (`docs/PROVIDERS.md`, dated observation 2026-07-24 — the ruler to reproduce):** three origins — Unirii (central) / Grozăvești (river barrier) / Berceni (periphery) — × 3 bands. Walk: corrected ORS ranges `[861,1744,2633]`s echoed exactly by ORS (verified live 2026-07-29); boundary residuals ±10%. Car: ruler = public OSRM `driving` + ORS-Matrix, boundaries straddle 1.0 within ±5% (free-flow). NEGATIVE KNOWLEDGE [0]: public OSRM `/foot/` is NOT pedestrian (returns ~35 km/h) — do not use it as a walk ruler; ORS rejects `Accept: application/json` with 406.

**Sizes (stated before pulling, per M045):** Geofabrik `romania-260824.osm.pbf` = **326,778,421 B ≈ 312 MB** (dated 2026-08-24). Machine: 16 cores, 496 GB disk free, **~3.5 GB RAM available** of 13 GB (owner apps using ~10 GB, 6.9 GB already in swap) → peak-RAM of a full-Romania Nominatim/ORS import is the binding constraint; addressed in the Work Plan.


## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Reviewed M045 (the only captured message) — it IS this task's directive; pulled its IN/OUT scope + acceptance bar verbatim in substance into ## Why (Owner scope). Ground truth for Intent = M045 + the P3 wording parked in tasks 007/009 (re-read there: 007 called this "P2 self-host OSM stack", 009 §OUT lists "self-hosting the engines (P2/P3 deploy)"; the owner's M045 label is P3 — same work regardless of label).

### Understand
- [x] Restate the request in my own words. What does the user actually want? — Author a docker-compose self-host stack (extend the existing `docker/`) that stands up Nominatim + Photon + ORS(foot+car) + a local pmtiles build, all from ONE Romania OSM extract; wire it to the app ONLY through the existing env config (no app-code change); document ops with REAL measured numbers; then prove — with measured parity numbers at the 2026-07-24 origins — that the app on the local stack matches the public providers within a stated tolerance, while the default (no-stack) config stays byte-identical and green.
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? — Solving exactly the stated stack. Temptations resisted: (a) touching app source to "improve" self-host behaviour — OUT, P1/P2 already did the config work and byte-identity forbids it; (b) MOTIS/transit — explicitly OUT (GTFS gate); (c) region-UI literals / multi-city — OUT (P4); (d) an ApiCache reaper — parked in 009, tempting but separate. My job is engines + parity + honest ops docs.
- [x] What would "done" look like? How will we know the task succeeded? — (1) `docker compose --profile selfhost up` (or documented equivalent) brings the four engines up from the Romania extract via documented import/build steps; (2) with `.env` pointing at them, `/api/geocode`, `/api/suggest`, `/api/isochrone` (walk), `/api/car` return valid results at the three audit origins, and the measured deltas vs the public providers sit inside a tolerance stated IN the task file with the actual numbers; (3) with NO stack + default env, `check:ci` + e2e are green and the app is byte-identical (proven: no `src/` diff); (4) ops facts (extract size, DB/graph/index disk, peak RAM, import wall-time) are measured and written down.
- [x] What are you assuming about the existing code/architecture that you haven't verified? — Assumed & VERIFIED from source: the three clients hit the paths above and read env bases (tasks 007/009). Assumed (to verify by running): mediagis/nominatim image serves `/search`+`/reverse` for a Romania PBF; ORS image serves `/ors/v2/isochrones/{profile}` with both profiles; Photon can import from the Nominatim DB and serve `/api`; planetiler produces a pmtiles the existing tiles route can Range-serve. Each assumption becomes a Work-Plan verification, not a claim.
- [x] What is OUT of scope for this task? — Transit/MOTIS self-host (GTFS licence gate — transit provider untouched); any prod/Railway deploy or config; any paid infra; weather/air-quality; multi-city UI; renaming Bucharest literals (P4); the ApiCache expiry reaper (parked in 009); ANY app `src/` behaviour change under default config (byte-identity is a hard acceptance criterion).

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. — **Combination: build (compose/scripts/docs) → measure (ops facts + parity) → assert (parity within tolerance).** Sequence: (A) author stack + gitignore + byte-identity proof [no heavy resources]; (B) plan panel; (C) **CHECKPOINT** — heavy imports are RAM/time-bound on the owner's active box (3.5 GB free) so before pulling GBs + hours of import I state sizes (done) and pick the lowest-disruption honest-parity path; (D) run imports, measure ops, measure parity; (E) impl panel + close. **Split decision:** kept ONE task — one subsystem, a panel reviews the whole compose+scripts+docs in one round, couplings mild (Photon→Nominatim; ORS/tiles independent). Rule 11's split trigger (server-contract AND client-rendering) NOT met — P3 is infra-only. May still split at the build/measure seam if the plan blows past ~15 gates; the CHECKPOINT is the reassess point. `tasks handoff` at the measure boundary if context runs high.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small. — Set **`assertive`** (field updated). LOAD-BEARING output = claims about the world: measured parity deltas + ops facts (sizes/RAM/times). Instrument to falsify: re-run the identical public-vs-local comparison at the same three origins (must reproduce within stated tolerance) + byte-identity tests + `check:ci`/e2e with no stack (green, zero `src/` diff). No data migration/secret/publish → not `irreversible`. Data gitignored; nothing pushed.

### Reflection Gates
- [x] Wrote task-specific check questions — (1) With the local stack up and `.env` set, does `GET /api/geocode?q=<Unirii address>` return a lat/lng within **≤ ~150 m** (≈ rooftop/parcel jitter between OSM geocoders) of the public Nominatim answer for the same query, at all 3 origins? (2) Does `GET /api/suggest?q=<3+ chars>` from local Photon return a non-empty, Bucharest-bbox-constrained list whose top hit matches the public Photon top hit's locality? (3) For walk `foot-walking` at each origin, are the local ORS isochrone ring areas within **±10%** of the public ORS ring areas (the same tolerance the 2026-07-24 audit used for boundary residuals) — measured as area ratio per band? (4) For `driving-car`, same ±10% area check per band vs public ORS. (5) With NO stack + default `.env`, is `git diff --stat` on `src/` empty, and are `check:ci` + e2e green (byte-identity half)? (6) Does the pre-commit hygiene grep stay clean over the new docker/scripts/docs? Each answer needs a measured number, not yes/no.
- [x] Test strategy: what are you testing and how? — **Two distinct kinds of proof.** (1) **Byte-identity / no-regression** = the existing suite: since P3 adds NO `src/` code, the *proof* that default config is byte-identical is `git diff --stat src/` empty + `check:ci` + e2e green with no stack. I will NOT add app unit tests (there is no app behaviour to test — adding some would move the test-count for no reason, violating rule 2). (2) **Parity** = a **committed, repeatable measurement harness**, not a hand-run one-off: a script (`scripts/selfhost/parity-check.mjs` or similar, under a self-host dir, NOT wired into `check:ci` since it needs the live stack) that hits both public and local endpoints at the 3 origins × 3 bands, computes geocode distance (haversine) + ring-area ratios (turf/area or a shoelace), and prints a table with pass/fail vs the stated tolerances. The task file records the ACTUAL numbers it produced (instrument-validated per rule 13: I hand-check one origin's numbers against a manual calc before trusting the table). Ops facts (disk/RAM/time) captured via `docker system df`, `/usr/bin/time -v`, `du -sh` on the gitignored data dirs — real command output pasted, not estimates.
- [x] Before the riskiest step: what would make you stop and reconsider? — The riskiest step is the heavy import on the owner's ACTIVE machine (3.5 GB free, already swapping). STOP triggers: (a) if a single import's RSS drives the box so deep into swap that the owner's session would be disrupted — prefer a bbox-clipped extract for the measured run (compose still targets full Romania) and document the clip + why it doesn't affect central-Bucharest parity; (b) if an import would exceed a few hours wall-time — reassess/handoff rather than block a session; (c) if Photon/ORS/Nominatim image versions don't serve the exact paths the clients call — fix the compose/env, never the app; (d) if parity is OUTSIDE tolerance — that is a real finding (report it honestly, investigate cause: profile/speed constant, extract coverage, ORS version), do NOT widen the tolerance to pass. Data must always land under a gitignored path — verify gitignore BEFORE any download.
- [x] If judging quality before building: is the gap worth closing? — Yes. This is the linchpin of the commercial pivot: P1/P2 built config knobs with nothing behind them; until the engines are proven to work self-hosted with measured parity, the whole "flat-cost self-host" plan is unvalidated. Counter-weight: real disk/time/RAM cost on the dev box, and ongoing maintenance of a compose stack. Both are acceptable — the stack is opt-in (profile/separate compose file), data is gitignored, and the default deployment is untouched (byte-identical). The alternative (ship the pivot without ever standing the engines up) is exactly the "instrument nobody runs" failure the calibration culture warns against.

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. — Reviewed. Likely growth point captured as W9 CHECKPOINT (full-Romania vs bbox-clipped measured run under RAM pressure) + explicit handoff option. Other likely growth: image-version path mismatches (ORS `/ors` prefix already noted in W5; Photon/Nominatim path verification is W11/W12). Added W6's public-only harness dry-run so the instrument is validated before a stack exists.
- [x] Does the work plan include moments where you stop and question your approach — not just execute? — Yes: W8 gates the whole heavy phase behind byte-identity + plan panel; W9 is an explicit CHECKPOINT/handoff before any GB is pulled; W15 forces an honest stop if parity is out of tolerance (investigate, don't widen). The build phase (A) is fully reversible and reviewable before committing machine resources.
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? — Yes. References carry the exact app contracts + parity provenance + sizes; the Work Plan is a linear runbook with per-gate checks; the CHECKPOINT/handoff is explicit. A fresh agent resuming via `tasks bootstrap` + this file could pick up at W9/W10.
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) — 18 gates across 3 phases + checkpoint; each is one reviewable unit of work with a concrete check. Slightly over the 6-8 "standard feature" count but that is expected for a build+measure+assert task and the CHECKPOINT reassess is in place per the >15-gate guidance.

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

> **Shape:** Phase A (author — no heavy resources) → byte-identity proof → **CHECKPOINT** → Phase D (imports + measurement) → Phase E (review/close). P3 adds NO `src/` code.


### Phase A — author the stack (zero heavy-resource risk)
- [ ] W1. **gitignore FIRST.**
- [ ] W2. [P5] `docker/selfhost/fetch-romania-extract.sh` written
- [ ] W3. [P6][P7][P9] Authored `docker/selfhost/docker-compose.yml` (SEPARATE file
- [ ] W4. Orchestration authored as small pinned scripts + compose (full command runbook lands in SELFHOST.md at W7)
- [ ] W5. `docker/selfhost/env.selfhost.example` written (a provider OVERLAY to merge into .env)
- [ ] W6. [P2][P3][P10] `docker/selfhost/parity-check.mjs` written (no deps, node ≥18 fetch; NOT in `check:ci`)
- [ ] W7. `docs/SELFHOST.md` written

### Byte-identity proof (default config, no stack)
- [ ] W8. [P8] **Byte-identity proven.** Working tree: only `M .gitignore` + new `docker/selfhost/` + `docs/SELFHOST.md`; `git diff --stat -- src/` **empty** (zero app-code change). Shell provider env grep **empty**; `.env` has no provider/extent overrides (public defaults apply). `npm run check:ci` (security:google-keys + lint + typecheck + test:coverage + build) **green**

### CHECKPOINT — reassess before heavy imports
- [ ] W9. [P4] **Reassessed.** Sizes stated (extract 312 MB, downloads acknowledged by owner); gitignore verified (W1); byte-identity green (W8). **Decision: proceed with the FULL-Romania measured run.** Rationale: owner explicitly wants "measured parity numbers", acknowledged "long first runs", and the imports are LOCAL + gitignored + reversible (`docker down -v`)

### Phase D — imports + measurement (heavy; full Romania; gitignored data)
- [ ] W10. Extract fetched
- [ ] W11. **Nominatim import done + verified.** Import wall-time **~24 min** (15:46:40
- [ ] W12. **Photon import + serve done + verified.** Pre-checked the DB was reachable (nominatim role auths over TCP scram-sha-256; `placex`=1,961,055 rows). `photon-import` (shares nominatim netns
- [ ] W13. [P6] **ORS graph build done + P6 verified.** Both profiles built from the extract
- [ ] W14. [P1][P9] **pmtiles built + schema-verified.** `build-tiles.sh` compiled the protomaps/basemaps generator from source (SHA `a50c699`) and produced the archive. **P1 CONFIRMED: source-layers = `boundaries, buildings, earth, landcover, landuse, places, pois, roads, water`
- [ ] W15. [P2][P3] **Parity measured — self-hosted stack matches public.** Two isolated app instances (public :3000 default env; local :3001 = self-host bases + `PROVIDER_DATA_REVISION=romania-260824`, keyless ORS, intervals 0). Harness self-test PASS first (rule 13). Car pin corrected mid-run: the time model is `preset=crowded|quiet` (task 059), not weekday/time


- [ ] W16. [P4][P5] Docs filled with MEASURED numbers, labelled full-Romania 2026-08-25

### Phase E — review / close
- [ ] W17. [P8] **Byte-identity re-proven + hygiene + mind-map done.** Restored the public 25 MB Bucharest tiles (removed the root-owned 684 MB build via docker, re-ran `tiles:fetch`
- [ ] W18. [owner decision 1
- [ ] W19. [owner decision 2
- [ ] W20. [owner decision 3


## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md

