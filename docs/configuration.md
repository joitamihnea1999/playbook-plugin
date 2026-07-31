# Configuration

Two JSON files, both under `.agent/` in your project and hand-editable: `config.json` is created by `/playbook:init`; `models.json` is deliberately NOT — create it with `tasks models select` (or by hand). `models.json` holds machine-specific judge pins and is gitignored by design.

`config.json` carries two kinds of setting, and the difference decides whether you commit it:

- **Review knobs** (`judge_budget_usd`, `review_timeout_secs`, `review_soft_timeout_secs`) are naturally per-install — a spend cap is a wallet decision and a timeout depends on the machine. Committing them just sets a default your teammates can override through the env tier — upward, in the timeout's case (see the floor rule below).
- **Project policy** (`merge_verify`) only works when it *is* committed: the merge skill reads it to decide whether a merge may auto-push, so every clone has to see the same declaration. A repo that leaves `config.json` untracked leaves that check permanently skipped.

## `.agent/config.json` — review knobs

```json
{
  "judge_budget_usd": 10,
  "review_timeout_secs": 1200,
  "review_soft_timeout_secs": 900
}
```

- `judge_budget_usd` — spend cap for the **claude** judge (`--max-budget-usd`). Default 10. Claude-only; codex/agy/grok/pi have no budget knob.
- `review_timeout_secs` — the **hard** timeout for every review agent (plan / impl / panel): hang safety only. On expiry the whole process tree is terminated, the judge's partial output is salvaged to `judge-*.partial.log`, and the prior review log is left untouched. Default 1200 — the 900s soft deadline plus five minutes of grace, so a judge winding down on schedule is never cut off mid-sentence. Set it to `0` or `"unlimited"` for **no wall-clock kill at all** — a judge that is still writing is then never cut off mid-response. (Other accepted unlimited spellings, as JSON *strings*: `"none"`, `"null"`, `"inf"`, `"infinite"`. A bare JSON `null` means "not set" and falls through to the default, as it does for every other key — use `0` if you mean unlimited.)
- `review_soft_timeout_secs` — the **soft** deadline, default 900. Keep it below the hard timeout; if it is higher it gets clamped down and warns on every review. This is not a kill; it is the number the judge is *told* about, instructing it to finish the thought it is in, then write its findings, and not open a new investigation branch. Set it to `0`/`"unlimited"` to drop the time paragraph from judge prompts entirely. If soft would exceed a finite hard, it is clamped down to hard so the prompt never promises more time than the process gets.

The hard number is a ceiling on the **judge subprocess**, not a guarantee about the whole command: after a kill the runner spends up to 5s reaping, and the antigravity panel seat is deliberately given `timeout_secs + 30` so agy reports its own expiry instead of being SIGKILLed. Budget ~1230s worst case rather than exactly 1200.

Two numbers rather than one because they answer different questions: soft is *when should the judge wrap up*, hard is *when is it obviously stuck*. Leaving only a hard kill means a judge doing good work gets truncated mid-sentence; leaving only a soft target means a genuinely hung process runs forever.

**Precedence, highest first:** CLI flag (`--budget`, `--timeout`, `--soft-timeout` on `plan-review` / `impl-review` / `panel-review`) → env var (`PLAYBOOK_JUDGE_BUDGET_USD`, `PLAYBOOK_REVIEW_TIMEOUT_SECS`, `PLAYBOOK_REVIEW_SOFT_TIMEOUT_SECS`) → `.agent/config.json` → built-in default. A missing file or malformed value falls back to the default (surfaced by `tasks doctor`, never fatal).

**One exception — `review_timeout_secs` in `config.json` is a floor, not just a tier.** When the file sets it, the CLI and env tiers may only *raise* the hard timeout, never lower it; a lower value is clamped up and a warning is printed. When the file sets it to unlimited, a finite `--timeout` is ignored outright. The reason is that `--timeout` is often passed by an agent, and an install that deliberately removed its kill window should not have one reintroduced by a subprocess argument. Nothing is floored when the file does not set the key — then ordinary precedence applies and `--timeout 60` means 60. The soft deadline is never floored; that is what `--soft-timeout` is for.

## `.agent/config.json` — `merge_verify` (project policy)

The `/playbook:merge` skill always verifies *the merge itself*: mind-map integrity, per-user contamination, and that the merge introduced no code of its own. Whether your *branches* are healthy is a different question, and only your project knows what answering it looks like — so you declare the command:

```json
{
  "merge_verify": {
    "command": "pnpm -r typecheck && pnpm -r test && pytest"
  }
}
```

- `command` — whatever "green" means for **this** repo. Point it at your full gate, not one layer's: a merge that runs only the backend suite can certify itself while the frontend is red.

Where and how it runs: from the repo root, on the **merged working tree after conflicts are resolved but before the merge commit is created** (so a command that inspects `git log`/`HEAD` sees the pre-merge target, not the merge). It runs via `bash` under `set -e -o pipefail`, so quoting is preserved, `&&` and multi-line commands work, and a failing *early* step fails the gate — without `set -e` bash reports only the last command's status, so `typecheck` failing followed by a successful `echo done` would report success. If your command writes to the tree (formatters, codegen), say so to yourself: the skill re-checks code identity afterwards, but a command that rewrites source during verification makes "the merge introduced no code" harder to assert.

Four outcomes, and the exit code is the verdict — only the first allows `--push`:

| Outcome | When | Effect on `--push` |
|---|---|---|
| **GREEN** (0) | declared command exited 0 | may auto-push |
| **FAILED** (1) | declared command exited non-zero | blocked |
| **BLOCKED** (2) | declared but unusable — malformed JSON, wrong shape, misspelled key | blocked |
| **SKIPPED** (3) | nothing declared: no file, no key, empty command | blocked; merge is presented for you to push by hand |
| **CONFIGURED** (4) | `--plan` only — a command exists but was deliberately not run | blocked (a classification is not a result) |

Two deliberate asymmetries:

- **Absent is not an error, but it is not a pass either.** With no `merge_verify`, the merge completes and reports honestly that no soundness command ran — then hands the push back to you rather than auto-pushing something nobody checked.
- **Broken is not the same as absent.** A misspelled `commnd` key blocks instead of silently skipping, because a typo must not quietly disable a gate you believe you declared. `tasks doctor` warns about these long before merge time.

Nothing is inferred on your behalf: if you declare no command, the skill will not guess one.

**Commit the file.** Nothing forces you to — `merge-verify.py` runs whatever `.agent/config.json` it finds on disk — but an untracked config means the gate exists only in your clone: your merges verify, everyone else's report SKIPPED. `tasks doctor` warns when a `merge_verify` is declared in a file git isn't tracking. And because the command is read from the *merged* tree, an incoming branch can change it — the same trust you already extend to running that branch's tests, but worth a `git diff "$target_before" -- .agent/config.json` when the branch isn't yours.

## `.agent/models.json` — judge panel pins

Judge selection lives in `models.json`: the plugin ships defaults in `provider/models.json`, and each install can shadow them per key with a gitignored `.agent/models.json`:

```json
{
  "default_judge": "claude",
  "panel": ["opus", "claude:claude-sonnet-5", "codex:gpt-5.5:xhigh", "agy", "grok:grok-4.5"],
  "aliases": {"opus": "claude:claude-opus-4-8"}
}
```

- `default_judge` — backend for bare `plan-review` / `impl-review`.
- `panel` — the judge seats for `panel-review`; each spec is `backend[:model[:effort]]`.
- `aliases` — shorthand names expanded before dispatch.

### Keeping pins alive

Pinned model ids rot as providers ship and retire models, so the pins have a maintenance loop:

- `tasks models check` audits every pin against **live availability**: codex pins are probed with a tiny prompt (the `~/.codex/models_cache.json` catalog alone doesn't prove your account can use a model), claude pins are probed budget-capped (claude has no list command — new ids enter via `--claude-candidates`), grok pins are checked against `grok models` (a login-aware entitlement list, so a listed pin is OK without a live turn), agy is unverifiable (`--model` is inert in `--print` mode; the judge always runs whatever model is selected in the agy UI). `--no-probe` is the free/fast degraded audit. Exits 1 when any pin can't run as configured.
- `tasks models select` refreshes interactively: shows the report, takes the new panel + default judge, writes `.agent/models.json` — creating it on fresh installs and preserving keys it doesn't manage.
- `tasks doctor` warns (never fails) on a missing models.json or dead pins, using the cheap checks only.

### Failure semantics

- When a review judge fails **specifically because its model no longer exists** — probe-confirmed, not just pattern-matched — the review still saves its output, then prints the availability report and exits nonzero: a deliberate hard stop so you re-pin before trusting a degraded panel. Timeouts, budget caps, and other errors keep their soft behavior.
- A judge that exhausts its budget cap is reported as **failed** with an explicit notice (raise `judge_budget_usd` or pass `--budget`) instead of masquerading as a successful empty review.

## Environment variables

| Variable | Purpose |
|---|---|
| `PLAYBOOK_JUDGE_BUDGET_USD` | Overrides `judge_budget_usd` (below CLI flags). |
| `PLAYBOOK_REVIEW_TIMEOUT_SECS` | Overrides `review_timeout_secs` (below CLI flags). May only raise a hard timeout that `config.json` has set — see the floor rule above. |
| `PLAYBOOK_REVIEW_SOFT_TIMEOUT_SECS` | Overrides `review_soft_timeout_secs` (below CLI flags). Not floored. |
| `PLAYBOOK_PROJECT_ROOT`, `PLAYBOOK_SESSION_ID`, `PLAYBOOK_SANDBOXED`, `PLAYBOOK_MINDMAP_MAX`, `PLAYBOOK_EVAL_CONFIG` | Internal — set by the wrappers, hooks, and sandbox; not meant to be set by hand. |
