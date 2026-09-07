# 014 - H1 Selfhost Merge Mindmap Devscript
## Status
pending

> **Before filling this in:** run `.claude/bin/tasks work <N>` to activate this task. Hooks won't enforce until activated.

## Risk
assertive

<!-- Set at Structure gate. Justification: (b) rewrites live-state CLAIMS in MIND_MAP node [0]
(and the now-false "live on Railway" clauses in [1]/[30]) and (c) adds docs/SELFHOST.md prose —
both are claims about the world. (a) local FF merge + (c) scripts are code-reversible, but the
task as a whole changes claims → assertive, needs an impl review to close.
Instrument that would reveal the claims false: mindmap_check.py (dated live-state claims,
node[0]-first, no phantom paths) + reading the real world (Railway expired → no prod; git shows
the FF merge landed; `npm run dev:selfhost --dry-run` exercises the script's preflight without
side effects). NOT irreversible: nothing is pushed or deployed, no data migrated, no secret rotated. -->


> **Set this at the Structure gate** to one of: `reversible` / `irreversible` / `assertive`.
> - `reversible` — the question is whether the WORLD reverts, not the diff: touches only code/config that `git revert` fully undoes. A change that deletes/migrates persisted data, alters an on-disk format, rotates a secret, rewrites history, or publishes/asserts a claim about the world is NOT reversible even when its diff reverts cleanly. Normal bar.
> - `irreversible` — deletes/migrates data, rotates a secret, rewrites history, or publishes. Needs a named rollback plan + explicit confirmation, and cannot light-close.
> - `assertive` — changes a **claim about the world** (docs, a calibration, a measurement, a "verified accurate"). Reviewed for the claim AND its instrument regardless of diff size — a docs-only diff can be the most review-worthy thing a task produces. Cannot light-close.
> - Leaving it `unclassified` is **not** the cheap way out: an unset gate cannot be evaluated, so the close is held to the same bar as `assertive`/`irreversible` (review evidence, or `--force --reason`). One word here is the whole cost.

## Intent
H1 housekeeping: (a) FF-merge self-host into HowFar main locally + verify green, no push; (b) correct MIND_MAP node [0] Railway/production claims to reflect no prod env, local selfhost stack until launch, Hetzner-at-launch hosting decision; (c) add npm run dev:selfhost up + matching down/stop + docs/SELFHOST.md

## Why
Railway trial EXPIRED — production is down; the map's node [0]/[1]/[30] still assert "live on
Railway" at a prod URL with a 2026-08-07 `/api/health` proof, which is now FALSE and violates the
owner's #1 priority (trust/honesty). The self-host branch carries P1–P4prep (tasks 007–013), all
local-only and unpushed; merging it into `main` is safe branch hygiene now that main deploys
nowhere. And the self-host stack is a multi-step staged runbook — a `dev:selfhost` one-command
convenience removes day-2 friction for running the app against the local engines.

## References
- [x] Context: read node [0] (current-state + rulings), [1] (overview, "running live on Railway at howfar-production-b31c.up.railway.app [30]"), [30] (Railway deploy config — railway.json/importer, still-committed mechanism), [23]/[25] selfhost basemap+bbox. HowFar repo: `self-host` is 15 commits ahead of `main`, main is an ancestor → **clean fast-forward**. Verify contract `_always` = `cd HowFar && npm run check` + `.agent/scripts` unittests; `assertive` adds mindmap_check + test_count_guard. Selfhost stack = `docker/selfhost/docker-compose.yml` (nominatim/photon/ors serve profiles), env overlay `env.selfhost.example`, staged runbook in `docs/SELFHOST.md`. App dev DB = root `docker-compose.yml` `db` on :5433.
- Playbook: playbook/Build
- Note: Don't hardcode task numbers in plans — `.claude/bin/tasks new` auto-increments.

### Recent Chat (auto-captured at activation — review and remove unrelated)

**[M052]** [2026-08-28 20:45:30]
Work under playbook discipline on HowFar (nested HowFar/ repo). H1 — three housekeeping items, one task (classify honestly): (a) merge branch self-host into main in HowFar/ (Railway has EXPIRED — prod...

---

## Design Phase

> **Write a 1-sentence answer for each gate.** A bare checkmark means you skipped it.
> Complete these gates before writing the work plan.
> (The `/playbook` skill has workflow patterns if you need a reference.)

### Chat Log Research
- [x] Only one message (M052), the H1 task itself — already distilled into Intent/Why. Key binding quotes: "do not push either branch — the owner pushes"; "never touch production"; hosting = "Hetzner VPS at launch, €0 until then"; "judges default grok:grok-4.6:high, codex :medium; launch reviews in the background"; "Stop after close with what changed."

### Understand
- [x] Restate the request in my own words. What does the user actually want? — Three housekeeping items, one task: (a) locally FF `main`→`self-host` in nested HowFar + confirm `npm run check` green, no push; (b) rewrite stale "live on Railway/prod" facts in MIND_MAP node [0] to truth (no prod env; runs locally on docker/selfhost until launch; hosting = Hetzner VPS at launch, €0 until then); (c) add `npm run dev:selfhost` (provider stack up + start app) + matching down/stop, documented in docs/SELFHOST.md.
- [x] Critique: Am I solving the stated problem or a different one I find more interesting? — Deliberately narrow housekeeping. The over-build temptation (make (c) a full first-run installer) is REJECTED: staged import/build is multi-hour + RAM-fragile; dev:selfhost is a day-2 convenience over the already-built stack, and must fail loudly rather than silently kick off a 24-min import.
- [x] What would "done" look like? How will we know the task succeeded? — (a) `git log main` shows self-host's tip + `npm run check` green + nothing pushed; (b) `mindmap_check.py` green, node [0] states the no-prod truth with an ISO date + the Hetzner ruling, no surviving false live-URL in [0]/[1]/[30]; (c) `dev:selfhost` + `dev:selfhost:down` resolve, scripts pass `bash -n`, a side-effect-free `--dry-run` runs the preflight, docs/SELFHOST.md documents both.
- [x] What are you assuming about the existing code/architecture that you haven't verified? — Verified this session: FF clean (is-ancestor=yes); built tiles+photon index exist on THIS box (⇒ preflight passes here, so verify via `--dry-run`, do NOT run full up); hooksPath armed; `.env*` gitignored (⇒ overlay must be inline-exported by the script, not a committed env file). Still to verify: `npm run check` green post-merge.
- [x] What is OUT of scope for this task? — Pushing either branch; any Railway/prod action; self-hosting transit (GTFS gate); the P4 region flip; the ApiCache reaper; rewriting README's Railway runbook (railway.json is still-committed config, not a false live-claim); making dev:selfhost a first-run importer.

### Structure
- [x] What kind of work is this? (build / investigate / evaluate / decide / combination?) If combination, what's the sequence? If >15 gates or uncertain approach, pick a checkpoint where you pause and reassess direction before continuing. — Combination, sequenced: (a) git merge → (b) doc/claim rewrite → (c) build dev tool + doc, so all HowFar code lands before the impl panel (freshness). Under 15 gates; independent verification seams (check / mindmap_check / bash -n+dry-run), no split needed.
- [x] **Set `## Risk`** (reversible / irreversible / assertive). Ask: if this claim/change were WRONG, what would show it? An `assertive` task (changes a claim about the world — docs, a calibration, a measurement) must name the instrument that would reveal the claim false. An `irreversible` task must name its rollback plan. These cannot be light-closed for being small. — Set `assertive`. Instruments: `mindmap_check.py` (undated live-state → non-zero) + reading the real world (Railway expired) for claims; `bash -n`+`--dry-run` for the script; `npm run check` for the merge. Rollback (not needed): `git branch -f main ff904f3`; mindmap/docs via `git revert`. No irreversible step — no push/deploy/data-migration.

### Reflection Gates
- [x] Wrote task-specific check questions (Bad: "is this working?" Good: "Does the output include the progress counter?" — the answer should require evidence, not just yes/no) — Q(a): is `main`'s tip `a45bd3e` and did it fast-forward (no new merge commit)? Q(a2): does `npm run check` exit 0 on main? Q(b): does `mindmap_check.py` exit 0 and `grep -c 'live-verified 2026-08-07\|running live on Railway' MIND_MAP.md` == 0? Q(c): does `npm run dev:selfhost -- --dry-run` finish with NO `docker`/`next` process started and print the preflight verdict? Q(c2): `git diff` shows `.env` unchanged?
- [x] Test strategy: what are you testing and how? (point tests for specific behavior, property tests via `hypothesis` for invariants on transformations/parsers/arithmetic) — Existing suites prove the merged code (`npm run check`); no new app unit tests (adds no `src/` behavior — scripts are ops glue). Executable proofs: `bash -n` (syntax), `--dry-run` (preflight logic, side-effect-free), `mindmap_check.py` (claim integrity). The `--dry-run` path IS the guard test — on THIS box prerequisites exist so it must still launch nothing.
- [x] Before the riskiest step: what would make you stop and reconsider? — Riskiest = the merge. Stop if `git merge --ff-only` refuses (unexpected divergence) — never fall back to a merge commit. Stop if `npm run check` goes red on main. Never `push`.
- [x] If judging quality before building: is the gap worth closing? — N/A (not a quality-judgement task); the dev:selfhost convenience gap is worth closing per the user's explicit ask.

### Verify
- [x] Review the work plan. If a likely growth point exists, add it to the plan now. — Growth point added: a "Pre-panel" gate (audit + PRE_PANEL_AUDIT sweeps). Likely further growth: docs may need a README script-table row (deferred unless the panel flags it). shellcheck NOT installed on this box (checked) → rely on `bash -n` + `--dry-run`.
- [x] Does the work plan include moments where you stop and question your approach — not just execute? — Yes: the `--ff-only` refusal stop, the `npm run check` red stop, and the impl panel before close. The (c) design has an explicit fail-loud-vs-silent-import decision point.
- [x] Checkpoint: Would a fresh agent understand this task and execute it well? — Yes: work plan names exact commands, files, and the FF target (a45bd3e), plus the no-push and no-.env-pollution constraints.
- [x] The work plan below has the right granularity (not too coarse, not micro-steps) — Yes: ~15 work gates across three independent seams, each with its own verification.

## Work Plan

> For each work section: what could go wrong? How will you know it worked? (specific check, not "looks good")
> Standard feature: 6-8 work gates + tests. Large tasks work fine — if >15 gates, add a mid-point checkpoint to reassess direction.

### (a) Fast-forward merge self-host → main (local only, no push)
- [ ] `tasks audit` clean + HowFar pre-commit hook armed (hook_sentinel) before touching git.
- [ ] `git checkout main` in HowFar; confirm main @ ff904f3 and clean tree.
- [ ] `git merge --ff-only self-host`
- [ ] Verify green on main: `npm run check` (security:google-keys + lint + typecheck + vitest + selfhost harness self-test). Record pass/counts.
- [ ] Confirm NOTHING pushed: `git status` shows `main` ahead of `origin/main`, no push performed.

### (c) dev:selfhost convenience (build first — code lands before impl panel)
- [ ] Write `docker/selfhost/dev-selfhost.sh`: preflight (docker present; built prerequisites `data/tiles/selfhost-romania.pmtiles` + `data/selfhost/photon/photon_data` + `data/selfhost/photon/photon.jar` present → else fail loud pointing at docs/SELFHOST.md §1–§5); `--dry-run` prints the plan + preflight result and exits with ZERO side effects; otherwise bring up app dev DB (root compose `db`) + provider serve stack (nominatim/ors/photon), wait for health with a bounded timeout (exit with a "still importing/building
- [ ] Write `docker/selfhost/stop-selfhost.sh`: `docker compose -f docker/selfhost/docker-compose.yml stop` (preserve named volumes
- [ ] Add `dev:selfhost` + `dev:selfhost:down` to HowFar `package.json` scripts.
- [ ] Verify: `npm run dev:selfhost -- --dry-run` runs the preflight side-effect-free; `bash -n` both scripts; `shellcheck` if available; confirm `.env` untouched.
- [ ] Document a "Day-2 dev loop (`npm run dev:selfhost`)" section in docs/SELFHOST.md: what it does, that it assumes the one-time import/build is done, preserves volumes, points env inline (doesn't pollute `.env`), and the down/stop path. Note transit still not self-hosted; app dev DB prerequisite.

### (b) Correct MIND_MAP node [0] (+ false live-claims in [1]/[30])
- [ ] Rewrite node [0] "Product state" line: replace the 2026-08-07 live/Railway proof with the dated truth
- [ ] Correct the now-false "running live on Railway at …railway.app" clause in [1]; add a dated retired-Railway note to [30] so nothing reads as currently-live. Keep railway.json config description (still committed).
- [ ] `python3 .agent/scripts/mindmap_check.py` green (no undated live-state claim, node[0] first, no phantom paths); `mindmap_check` after every MIND_MAP edit per CLAUDE.md.

### Pre-panel
- [ ] `tasks audit` + `.agent/PRE_PANEL_AUDIT.md` sweeps (stale-claim grep, publication hygiene over git diff + untracked, test-count) recorded here.

---

## Pre-review
- [ ] All tests pass
- [ ] No debug artifacts
- [ ] MIND_MAP.md: update the OWNING subsystem node **in place**

