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

## Tier 2 — live A/B trial (expensive, explicit): *are outcomes better?*

Tier 1 only says a decision *changed*, not that it's *better*. `arena/trial.py`
answers efficacy: run a real agent on a frozen **case** under the baseline
harness and the treatment harness, N reps each, score each run by the case's own
deterministic `check`, and apply a **frozen decision rule** → `ADOPT` (treatment
passes more), `REJECT` (fewer), or `RETEST` (equal / an arm never executed).

```
python3 arena/trial.py <case-dir> [--baseline REF] [--treatment REF] \
        [--reps N] [--max-runs M] [--runner sandbox|null] [--agent A] [--ledger F] --yes
```

It **spends real tokens and wall-clock, so it is never automatic**: without
`--yes` it only prints the run estimate and stops; `--max-runs` (default 6) is a
hard budget ceiling; every run is appended to a `--ledger` JSONL.

Where the agent work comes from is a **runner**, so the verdict logic is
provable without an agent:
- `--runner null` — does nothing; the shipped `cases/canary-noop` case returns
  `RETEST` (identical arms), proving the whole pipeline runs offline and free.
- `SandboxRunner` (default) — the live path: wires each arm's workspace hooks to
  that arm's extracted harness variant and launches `scripts/sandbox` headless.
  Validate on first real use (agents must be configured); its ADOPT/REJECT/RETEST
  logic is exactly what the tests exercise via a scripted `FakeRunner`.

### Cases

`cases/<name>/`: a `case.json` (`{name, agent, prompt, workspace, check, reps}`)
plus a `workspace/` template copied fresh into every run. `check` is a shell
command run in the resulting workspace; exit 0 = success. A good case is small,
deterministic, and exercises the behavior the harness change is meant to affect.

## Tests

`python3 arena/test_replay.py` (Tier 1) and `python3 arena/test_trial.py`
(Tier 2) — 13 self-tests, all offline/free, incl. the negative control (replay
detects the real 1.5.10 MultiEdit change) and the full ADOPT/REJECT/RETEST rule.

## Why it's structured this way

Running real agents is the only way to measure outcomes, and it costs real
tokens and wall-clock. So the design never runs them unless the free tier first
proves the change actually alters what an agent experiences. Cheap filter →
paid measurement, gated on real signal plus consent.
