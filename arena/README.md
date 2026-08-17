# Arena — measuring whether a harness change actually helps

A **development tool** for `playbook-plugin` itself. It lives beside `tests/` and
is **never shipped** in `plugins/playbook/` — a `playbook:init` project never sees
it. Its job: when we change how the harness drives agents, tell us whether the
change *helps*, instead of believing it does.

It is dormant and free by default, and tiered from cheap to expensive so we only
ever pay for a measurement that's actually warranted.

## Tier 1 — replay (free, automatic): *did any decision change?*

`arena/replay.py` — a playbook hook is a pure function of (project state, tool
payload) → decision (`exit 2` = BLOCK, `exit 0` = ALLOW). Each fixture in
`fixtures/*.json` records one such input. Replay runs it through **both** the
baseline hooks (extracted from a git ref via `git archive`) and the working-tree
hooks, and diffs the decisions.

```
python3 arena/replay.py [--baseline REF] [--fixtures DIR] [--json]
```

- Exit 0 — no decision changed → **nothing to measure, no trial warranted.**
- Exit 1 — decisions changed → a live A/B trial *may* be worth its cost.
- Exit 2 — the replay itself could not run.

This is the whole "only when needed" gate: most harness changes either change no
decision (stop here, free) or change one we already have a test for. No agents,
no tokens. Example — replaying against the commit before MultiEdit was gated
correctly surfaces the one changed decision:

```
[≠ CHANGED] MultiEdit-code-without-task: baseline=ALLOW working=BLOCK
→ behavioral delta detected; a live A/B trial (Phase 2) may be worth its cost.
```

### Automatic trigger (only when needed)

`playbook-plugin-dev` isn't a `playbook:init` project (it's developed with git +
the test suite), so the replay fires via a **git pre-push hook** — install once:

```
bash arena/install-hooks.sh
```

On every `git push` the hook (`arena/githooks/pre-push`, version-controlled;
`.git/hooks/pre-push` is a shim that execs it) checks whether the pushed commits
touch harness files (`plugins/playbook/scripts|provider/`). **Only if they do**
does it run the replay (baseline = what's on the remote, treatment = what's being
pushed) and print the verdict. It is **advisory** — it never blocks a push; a
behavioral change is often intended, and this is a heads-up, not a gate. A push
with no harness change prints "skipped" and costs nothing.

Once `playbook-plugin-dev` adopts `playbook:init`, this can move to the cleaner
`tasks work done` trigger (run the replay when a closing task's diff touched the
harness).

### Fixtures

`fixtures/*.json`, each: `{name, hook, files{}, current_state, payload}`. `files`
seeds a throwaway project (`.agent/tasks/` makes it a playbook project);
`current_state` is the active-task pointer; `payload` is the hook's stdin JSON.
Add a fixture for every decision worth protecting from silent drift.

## Tier 2 — live A/B trial (expensive, explicit): *are outcomes better?* (not built yet)

Tier 1 only says a decision *changed*, not that it's *better*. Tier 2 answers
efficacy: run a real agent (via the existing `scripts/sandbox` one-shot headless
runner) on a frozen case under the baseline vs the changed harness, score with the
judge panel, aggregate → ADOPT / REJECT / RETEST. Budget-capped and never
auto-invoked — it prints an estimated cost and waits for an explicit go. Planned,
not yet implemented.

## Why it's structured this way

Running real agents is the only way to measure outcomes, and it costs real
tokens and wall-clock. So the design never runs them unless the free tier first
proves the change actually alters what an agent experiences. Cheap filter →
paid measurement, gated on real signal plus consent.
