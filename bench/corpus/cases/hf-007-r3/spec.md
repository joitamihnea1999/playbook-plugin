# 007 - Phase1 Config Lift Providers And Extent
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
reversible

> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
COMMERCIAL SELF-HOST REBUILD — PHASE 1 (START HERE). Goal set by owner 2026-08-17 (MIND_MAP node [0] ruling 9): HowFar must cost NOTHING to develop (whole stack runs locally in Docker on free OSM/GTFS data + open-source engines) and, once live, cost ONLY a server (~€50/mo Hetzner-class box, flat regardless of users) + a domain (~€12/yr) — no per-call/per-user API fees, no data fees. Achieved by self-hosting every currently-remote provider on ONE Geofabrik Romania OSM extract (+ per-city GTFS for transit), for ALL Romanian cities, not just Bucharest. Weather/air-quality deferred. Payment-processor % is the only other cost, and only once users pay. PHASE 1 (this task) is the prerequisite that makes free->paid and Bucharest->all-Romania a config change, not a rewrite: lift the hardcoded provider hosts (src/features/search/server/nominatim.ts BASE, photon.ts BASE, isochrones/server/ors.ts HOST, transit.ts + transit-plan.ts URL, amenities Overpass endpoint lists) into src/lib/env.ts as env vars each DEFAULTING to today's public host (zero-behavior-change refactor), AND lift the extent/region (src/app/api/tiles/route.ts data/tiles/bucharest.pmtiles path + the Bucharest launch-bbox geofence in src/lib/bounds.ts) into config. Result: dev=prod differs only by env; pointing at self-hosted instances or a different city becomes an .env edit. LATER PHASES (separate tasks, do NOT do here): P2 self-host OSM stack (Nominatim/Photon/ORS/tiles) from the Romania extract + replace weekly public-Overpass amenity import with a local import; P3 self-host MOTIS on Bucharest GTFS, retire Transitous, feed-list as config; P4 flip extent to all-Romania + verify a 2nd city; P5 go-live (provision box, domain, TLS, live-verify). BLOCKING GATE before P3: verify the Bucharest GTFS feed (STB/TPBI via the Mobility Database / Transitous ro.json) license permits COMMERCIAL use — each city is a separate go/no-go. Product feature task => full 7-seat panel bar applies. Cut a long-lived branch (e.g. self-host) in HowFar/ so live main stays deployable.

## Why
Owner ruling 9 (MIND_MAP [0], 2026-08-17): HowFar becomes a paid, self-hosted, multi-Romanian-city product. The whole later plan (self-host the OSM stack + MOTIS, flip Bucharest → all-Romania) is only cheap if pointing at a self-hosted instance or a different city is an **env edit, not a rewrite**. Today provider hosts are hardcoded constants in the feature clients and the extent is hardcoded to Bucharest in three places. Phase 1 lifts both into config with **defaults equal to today's values**, so the current prod deployment is byte-for-byte unchanged and every later phase becomes a config flip.

## References
- [x] Context — pulled nodes [0], [18], [23], [25] via `tasks recall`; excerpts below. Confirmed the exact hardcoded sites by grepping `src/` for provider hosts.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.

**Node [18] Provider Client Template** — every client has a constants block with endpoint/host/interval/timeout/TTL kept in code next to the client. Shared plumbing in `lib/provider-http.ts` (per-host rate limiter keyed on `rateHost`, `ProviderError`, cache-key helpers). Clients to lift: nominatim, photon, ors, transit, transit-plan, the two Overpass pools.

**Node [23] Self-Hosted Basemap** — `/api/tiles` serves `data/tiles/bucharest.pmtiles` with HTTP Range. The tile extract `--bbox` in `scripts/fetch-tiles.sh`, the geocoder geofence, and MapLibre `maxBounds` are the **same box** and must stay in sync. (protomaps.github.io glyphs/sprite are a separate keyless-static-asset exception — global, not region-specific — OUT of scope, see Parked.)

**Node [25] Geofence & Bounds** — `lib/bounds.ts` is **isomorphic (no server-only imports)**: server uses `inBucharest` to geofence, client uses `BUCHAREST_MAX_BOUNDS` for MapLibre `maxBounds`. Same box appears in three places (bounds, fetch-tiles.sh, and the tile extract). ⇒ the bbox must be reachable from client code, so it needs a `NEXT_PUBLIC_*` (build-time-inlined) var, NOT the server-only `serverEnv()`.

### Hardcoded sites confirmed (grep of `src/`, non-test):
- `search/server/nominatim.ts` `BASE`/`HOST` = nominatim.openstreetmap.org
- `search/server/photon.ts` `BASE`/`HOST` = photon.komoot.io/api
- `isochrones/server/ors.ts` `isoUrl`/`HOST` = api.openrouteservice.org
- `isochrones/server/transit.ts` `URL`/`HOST` = api.transitous.org/api/v6/one-to-all
- `isochrones/server/transit-plan.ts` `URL`/`HOST` = api.transitous.org/api/v1/plan
- `amenities/server/overpass-client.ts` `ENDPOINTS[]` (3: maps.mail.ru, overpass-api.de, kumi)
- `amenities/server/bulk-overpass.ts` `BULK_ENDPOINTS[]` (2: overpass-api.de, kumi)
- `app/api/tiles/route.ts` `TILES_PATH` = cwd/data/tiles/bucharest.pmtiles
- `lib/bounds.ts` `BUCHAREST_BBOX` = 25.8,44.2,26.4,44.7
- `scripts/fetch-tiles.sh` `BBOX`/`OUT` (third corner of the same box)

---

## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Review the "Recent Chat" messages captured in References (auto-injected at `tasks work`). Remove unrelated ones. Pull key user quotes, constraints, and context into Intent/Why above. The user's actual words are the ground truth for Intent. — M038 is this task verbatim; M036/M037 are workspace/harness background. Distilled binding constraints into Why: "No behavior change for the current Bucharest deployment — same values move"; "dev and prod differ only by environment"; don't touch prod; don't push HowFar without owner approval; work on a branch.

### Understand
- [x] Restate the request in my own words. What does the user actually want? — Lift every hardcoded remote-provider host + the hardcoded Bucharest extent out of code into env-driven config, each var DEFAULTING to today's value, so behavior is byte-identical with no env set and switching host/city becomes an `.env` edit.
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? — The over-reach temptation is to start self-hosting or rename `BUCHAREST_*`. Both are later/cosmetic. Solving exactly the config-lift: a pure refactor.
- [x] What would "done" look like? How will we know the task succeeded? — (a) all listed sites read config; (b) no new env ⇒ every existing unit + e2e green and URLs/geofence byte-identical (ors.test URL assertion + a new default-equality test); (c) `.env.example` documents each knob; (d) a test proves an override is honored AND a malformed value falls back to default; (e) MIND_MAP [18][23][25][0] updated.
- [x] What are you assuming about the existing code/architecture that you haven't verified? — Verified: bounds.ts is isomorphic (no server imports, imported by client map) ⇒ bbox needs `NEXT_PUBLIC_`; `new URL(base).host` reproduces every current `HOST` (checked each); provider-http uses only `rateHost`; `fetchBulkOverpass` already accepts `endpoints`; cache keys are coord/query-hash based, not host-based ⇒ host change never poisons cache.
- [x] What is OUT of scope for this task? — Self-hosting (P2–P5); protomaps glyphs/sprite (global keyless assets); renaming `BUCHAREST_BBOX`/`inBucharest` (cosmetic, → P4); MOTIS/ORS path structure (version-specific, stays in code); GTFS licensing (P3 gate).

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. — Build: mechanical config-lift across ~10 code files + `.env.example` + fetch-tiles.sh + tests + mind map. One verification seam ("default == today"), reviewable whole (rule 11, no split). Checkpoint after WG1 (config module defined + tested) before wiring the 8 call sites.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small. — Set `## Risk` = **reversible**: only code/config + docs-of-our-own-code, no persisted data / on-disk format / secret / history / external claim; `git revert` fully restores the world (defaults keep prod byte-identical, cache keys are host-independent so nothing lingers). WRONG would show as a default ≠ today, caught by ors.test's byte-exact URL assertion + a new default-equality test + full e2e. NOTE: `panel_required_for: all` ⇒ quorum-PASS **impl** panel still required to close; product policy ⇒ plan+impl both run.

### Reflection Gates
- [x] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" — the answer should require evidence, not just yes/no) — (Q1) No new env ⇒ does `providerConfig()` return today's hosts and the ORS URL stay byte-exact? (Q2) Does `new URL(base).host` == the old `HOST` for all 6 providers? (Q3) `NOMINATIM_BASE_URL=https://nom.example` ⇒ does the request AND the rate-limit host both become `nom.example`? (Q4) A new `NEXT_PUBLIC_MAP_BBOX` ⇒ `inBucharest`/`BUCHAREST_MAX_BOUNDS` reflect it, and a malformed value falls back to 25.8/44.2/26.4/44.7? (Q5) `npm run check` + full e2e green with no env set?
- [x] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic) — Point tests: (a) `parseProviderConfig` in env.test.ts — defaults == today's literals, override honored, list-split + trailing-slash strip; (b) `tilesPmtilesPath` default vs override; (c) new pure `parseBbox` helper in bounds.ts — parse + malformed→null fallback (watched-fail: mutate a default, see equality test red, restore). Existing client tests (ors/transit/transit-plan/overpass/nominatim/photon) are the byte-identity regression net. e2e = full Playwright on the unchanged Bucharest build.
- [x] Before the riskiest step: what would make you stop and reconsider? — Riskiest = isomorphic bbox (client build-time inlining) + the `URL` local-const shadowing in transit*.ts. Stop triggers: bounds.test relative invariants red; any client URL assertion changes; a `next build` failure implicating top-level `process.env`; `new URL(default).host` ≠ old HOST anywhere.
- [x] If judging quality before building: is the gap worth closing? — N/A (build task); the "gap" is that config isn't lifted yet — closing it is the task.

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. — Likely growth point captured: WG9 (fetch-tiles.sh) is the third corner of the "one number set" invariant — added so the config-lift is coherent, not half-done. Also pre-empted the naming smell (WG7/Parked).
- [x] Does the work plan include moments where you stop and question your approach — not just execute? — Yes: WG1 ends with an explicit CHECKPOINT to confirm the config-module shape before wiring 8 call sites (cheapest place to change direction).
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? — Yes: locked design decisions + per-gate check + exact var names + the byte-identity invariant are all written down; a fresh agent could execute WG1–WG11 without re-deriving.
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) — One gate per file/seam (11 gates), each independently verifiable; under the 15-gate split threshold and single verification seam, so no split (rule 11).

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

**Design decisions (locked before coding):**
- **Provider hosts** (all server-side, in `server/` folders) → a new **optional-only, NON-memoized** `providerConfig()` in `lib/env.ts`, read **inside the request functions** (never at module top level —), separate from `serverEnv()` so provider clients never newly depend on required `DATABASE_URL`/`AUTH_SECRET`. Each field defaults to today's literal. Granularity = **base URL per provider** (paths like `/v2/isochrones/{profile}`, `/api/v6/one-to-all` are provider-version-specific, stay in code). Rate-limit host derived via `new URL(base).host` (verified == every current `HOST`).
- **Fail-closed validation** (matches `env.ts` `required()`): absent var ⇒ default; a var that is SET but invalid ⇒ `EnvError`. Invalid = non-`http(s)` / unparseable URL; empty endpoint pool; bbox that is non-finite, mis-ordered (min≥max), or span > 2° either axis (single-city cap).
- Env var names: `NOMINATIM_BASE_URL`, `PHOTON_BASE_URL`, `ORS_BASE_URL`, `TRANSIT_BASE_URL` (one host feeds both one-to-all + plan), `OVERPASS_ENDPOINTS`, `OVERPASS_BULK_ENDPOINTS` (comma/space-separated lists), `TILES_PMTILES_PATH`. Extent bbox: `NEXT_PUBLIC_MAP_BBOX` = `"minLng,minLat,maxLng,maxLat"`.
- **Cache-safety on switch**: a `configCacheTag()` = **empty string when resolved config deep-equals defaults** (⇒ default keys byte-identical, 30d cache still valid), else a short sha; prepended uniformly to **all 9 provider-dependent ApiCache key families** — nominatim fwd/rev, photon suggest, ors foot/car, transit, transit-plan. The tag's bbox component reads the RESOLVED `BUCHAREST_BBOX` (build-consistent), not a runtime env read. Flipping a host/bbox then serves a fresh namespace instead of stale old-provider/old-city answers.
- **Extent bbox** stays in `lib/bounds.ts` (isomorphic) reading `NEXT_PUBLIC_MAP_BBOX` **directly** (build-time inlined — honest constraint: changing the extent needs a rebuild; provider hosts are runtime) via a pure tested `parseBbox` + validation, fallback = current Bucharest numbers. **Single-city extent** by design (per-city GTFS/MOTIS arch). Export names `BUCHAREST_BBOX`/`inBucharest`/`BUCHAREST_MAX_BOUNDS` **kept** (rename → P4, Parked). Photon focus point derived from bbox centre (terra).
- **Tiles path** server-only → `tilesPmtilesPath()` in `lib/env.ts`, default = `cwd/data/tiles/bucharest.pmtiles`.
- Zero-behavior-change invariant, proven by LITERAL-VALUE tests, not self-referential ones: with no new env every URL/host/geofence/cache-key is byte-identical.

### Work gates
- [ ] **WG1 — config module + validation + cache tag.** Add to `lib/env.ts`: default consts; `parseProviderConfig(source)` (optional-only; trim; trailing-slash strip; list split on `[,\s]+` dropping empties; **fail-closed**: SET-but-invalid URL/scheme or empty pool ⇒ `EnvError`); `providerConfig()` = parse(process.env) **non-memoized**; `tilesPmtilesPath(source)`; `configCacheTag(source)` (""==default else short sha). Tests: defaults == today's literals (explicit), override honored, list-split, strip, tiles default/override, cache-tag ""-on-default + distinct-on-override + no-collision, negative validation (bad scheme, empty pool). Check: env.test.ts green; `parseProviderConfig({}).nominatimBase === "https://nominatim.openstreetmap.org"`. **CHECKPOINT: confirm the shape here before wiring call sites.**
- [ ] **WG2 — nominatim.ts.** Read `providerConfig().nominatimBase` + `new URL(base).host` inside `geocode`/`reverseGeocode`; prepend `configCacheTag()` to the two keys. Add a URL-assertion test (mirror ors.test:201) proving default URL byte-exact + an override test proving request URL AND rateHost move. Check: nominatim.test green; no literal host left.
- [ ] **WG3 — photon.ts.** Pure host lift (focus derivation PARKED to P4
- [ ] **WG4 — ors.ts.** Lift `isoUrl` base + `HOST` from `orsBase` inside the fetch fn; prepend cache tag to foot+car keys. Check: ors.test:201 still byte-exact; add override test; ors.test green.
- [ ] **WG5 — transit.ts + transit-plan.ts.** Rename local `const URL` (shadows global `URL`)
- [ ] **WG6 — overpass.** `overpass-client.ts`: `ENDPOINTS` from `overpassEndpoints` (host via `new URL(url).host`); `bulk-overpass.ts`: `BULK_ENDPOINTS` default from `bulkOverpassEndpoints`. Check: overpass-client.test + bulk-overpass.test green (3 / 2 endpoints by default); add a default-list-equality test.
- [ ] **WG7 — bounds.ts extent.** Add pure `parseBbox(raw)` (finite + min<max + span≤2° or null) ; `BUCHAREST_BBOX = parseBbox(process.env.NEXT_PUBLIC_MAP_BBOX) ?? DEFAULT`. Tests: explicit `toEqual({25.8,44.2,26.4,44.7})` with no env; parseBbox parse + each malformed→null case; watched-fail on a mutated default. Check: bounds.test green.
- [ ] **WG8 — tiles route.** `TILES_PATH` from `tilesPmtilesPath()`. Check: route resolves the same path with no env; covered by e2e (+ a unit test if cheap).
- [ ] **WG9 — fetch-tiles.sh.** `source .env` if present; read `NEXT_PUBLIC_MAP_BBOX` (SAME name, same format) for `BBOX` and `TILES_PMTILES_PATH` for `OUT`, both defaulting to today's values; `mkdir -p "$(dirname "$OUT")"`; update "keep in sync" comments to name the env var. Check: `bash -n` parses; no-env run yields the same bbox/out.
- [ ] **WG10 — .env.example.** Document every new var under a "Region / self-host" section (today's public defaults, commented; note bbox is build-time / hosts are runtime; single-city extent). Check: hygiene grep clean.
- [ ] **WG11 — verify + mind map.** Rebuild `.next`, `fuser -k 3799/tcp`, run `npm run check` + full e2e. Update MIND_MAP [18] (config-driven hosts + cache tag), [23] (tiles path + fetch-tiles env), [25] (bbox env + single-city + names-kept), [0] status (Phase 1 done). Run `mindmap_check.py`. Check: all green; publication-hygiene grep over whole diff + untracked.


## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md

