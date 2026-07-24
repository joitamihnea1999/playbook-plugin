# Changelog

Notable changes to the playbook plugin. Follows [Keep a Changelog](https://keepachangelog.com/) loosely; maintained by the README audit skill (entries before 1.4.2 are reconstructed from git history and the project mind map).

## [1.4.5] — 2026-07-25

### Changed
- **The merge skill no longer hardcodes one project's layout or test runner** (field report, AloVet 2026-07-24; task 021). Step 7's verification bundle assumed a `backend/` directory and a `run-backend-tests` command, which contradicted the skill's own claim to need only `.agent/` and the two mind-map files. On a repo without them the code-identity check diffed a directory that didn't exist — reporting green for having examined nothing — and the test step errored outright. Both are now repo-agnostic:
  - **Code identity** diffs the whole tree minus the paths the semantic steps own (`git diff "$target_before" -- . ':(exclude)MIND_MAP.md' ':(exclude)MIND_MAP_OVERFLOW.md' ':(exclude).agent'`). This covers code wherever it lives, including root-level files like `package.json` or `main.py` that a configured directory list would miss, and an empty result now truthfully means "the merge introduced no code."
  - **Project soundness** runs the command the project declares in `.agent/config.json` as `{"merge_verify": {"command": "…"}}`, via the new bundled `merge-verify.py`. Nothing is inferred: declare no command and the skill says so instead of guessing one.
- **`.agent/config.json` is now documented as committable**, with two tiers of ownership: review knobs (`judge_budget_usd`, `review_timeout_secs`) stay per-install and overridable through `PLAYBOOK_*` env vars, while `merge_verify` is project policy that only works when every clone sees it. `merge-doctor` treats a tracked `config.json` as correct rather than legacy detritus — previously it demanded `git rm --cached` on it, which would have failed the merge skill's own Step 7(b) gate for any repo that followed the new instruction to commit the file.

### Added
- **`skills/merge/merge-verify.py`** (pure stdlib, ships with the skill). Runs the declared command via a temp script so quoting is preserved (`pytest -k 'not slow'` keeps its meaning), and reports a four-way verdict through its exit code: **0** GREEN, **1** FAILED (the command's own rc appears in the status line), **2** BLOCKED (declared but unusable), **3** SKIPPED (nothing declared). `--plan` classifies without running; `-C` sets the project root.
- `tasks doctor` now warns on an unusable or empty `merge_verify`, using the same rules the merge enforces (doctor loads the skill's resolver rather than keeping a second copy that could drift).

### Migration
- **Repos that relied on the old hardcoded gate must declare `merge_verify.command`.** Without it a merge still runs and verifies itself, but reports `SKIPPED` for project soundness and **will not auto-push with `--push`** — it stops and hands the push to you with the situation stated. This is deliberately stricter than the field report proposed (which suggested a skipped check should not block): on the day this ships every repo is unconfigured, so a non-blocking skip would mean `--push` auto-pushing with zero soundness verification — the exact failure the report was written about. Point `merge_verify.command` at your **full** gate, not one layer's; a merge that runs only the backend suite can certify itself while the frontend is red (the incident behind this change).

## [1.4.4] — 2026-07-23

### Fixed
- **Hook commands now resolve on Grok Build** (field report, AloVet 2026-07-20; task 019). Every `hooks.json` command shipped quote-wrapped (`"${CLAUDE_PLUGIN_ROOT}/scripts/<hook>"`); Claude Code runs hook commands through a shell and tolerated it, but Grok Build resolves a space-free command as a literal *path* relative to `hooks/`, keeps the quotes, and fails command-not-found in 0ms — silently fail-open for all six hooks (gate enforcement, state-echo, chat-log, and the session hooks all off while the CLI still worked). Commands now ship the dual-host form `bash "${CLAUDE_PLUGIN_ROOT}/scripts/<hook>"`: the leading `bash` forces Grok's inline-shell resolution (quotes honored) while keeping a spaced plugin root a single argument on Claude Code. This **reverses the 1.4.0 note** ("Hook commands quoted … no longer fail silently under providers that word-split") — that fix was based on a "Grok word-splits like a POSIX shell" model the field report falsified; real Grok path-resolves, it does not word-split, so bare quoting made things worse, not better.
- **Grok host integration (task 020):** on spaced project paths (e.g. iCloud), Grok never schedules project/plugin hooks — only global `~/.grok/hooks`. `GrokAdapter.install_hooks` now writes always-trusted `~/.grok/hooks/playbook-enforcement.json` with absolute `bash "/path/to/script"` commands so task-gate/state-echo/chat-log actually run. PreToolUse matchers include Grok tool names (`write`, `search_replace`, `run_terminal_command`); payload normalizer maps those names to Claude Edit/Write/Bash.
- **Grok init always installs enforcement hooks** — `tasks init --provider grok` auto-calls `install_hooks` (docs no longer omit the only reliable channel). Atomic write for the global file; mirror-aware plugin-root resolution; normalizer remaps foreign tool names even without camelCase dialect markers.

### Added
- `tasks doctor` hook-command check: scans every hooks.json copy the host might load (`CLAUDE_PLUGIN_ROOT`, the copy beside the running module, the workspace source tree, and Grok's own `~/.grok` installed/marketplace copies) and warns on any quote-wrapped command, missing registration, or missing referenced script — so a stale installed or Grok-side copy is caught even when the source tree is clean.
- `tasks doctor` Grok enforcement check: warns when `~/.grok/hooks/playbook-enforcement.json` is missing (if AGENTS.md present) or its baked script paths no longer exist after upgrade/move.

## [1.4.3] — 2026-07-20

### Security
- **Judge isolation**: panel and single-judge reviews now run the judge process read-only (`project_writable=False`) so a misbehaving judge cannot mutate the repo or task files. A repo-wide tamper guard (`git status --porcelain` + task.md hash, before/after) is the backstop on platforms without OS containment (Windows/nested): on a detected change the verdict is still saved with a loud TAMPER banner, task.md ingestion is refused, and the run exits non-zero.

### Added
- `tasks doctor` gate-logging check: scans every lane's `chat_log.md` (not just the current user's) and warns when gate entries stop while tasks keep completing — the silent retro-fidelity loss from a stalled `state-echo-hook`.

### Fixed
- `tasks global-retro-collect` now discovers and collects the multi-user `.agent/<user>/` layout (per-user tasks + chat logs), with lane-tagged manifest entries so duplicate task numbers across users stay distinct. Single-user root repos are unchanged.
- `state-echo-hook` gate logging is now fail-open and fail-loud: a write failure (e.g. a Windows AV lock on the counter file) no longer silently kills the hook under `set -e`; it surfaces a warning instead (suppressed inside sandboxed judges).
- `tasks log` parses chat-log entries again — the `(provider/pid)` header suffix added by multi-provider tagging had silently broken its regex (zero output); the provider is now shown in the agent column.
- Retro bare-checkmark heuristic no longer false-positives on gates annotated with indented continuation lines (numbered sub-bullets, `→` lines).

## [1.4.2] — 2026-07-17

### Added
- README audit: maintainer skill (`.claude/skills/readme-audit/` in this repo, not shipped with the plugin), README-drift advisory in `tasks doctor` and `tasks bootstrap` (maintainer checkouts only), audit baseline at `docs/readme-audit-baseline.json`.
- Layered documentation: `docs/cli.md`, `docs/configuration.md`, `docs/providers.md`, `docs/architecture.md`; README rewritten user-first with the deep material moved there.

## [1.4.1] — 2026-07

### Fixed
- `tasks models check` now discovers current Claude models on fresh installs (reads the model your Claude Code is configured to run — Claude has no CLI list command); shipped alias table refreshed.

## [1.4.0] — 2026-07

### Added
- **Grok** as the fifth provider (judge + main agent): `playbook-grok` launcher, native hook discovery with a shared payload-normalization shim, entitlement-aware `models check` support.

### Fixed
- Hook commands quoted in `hooks.json` — plugin installs under paths with spaces (e.g. iCloud checkouts) no longer fail silently under providers that word-split. _(Superseded by 1.4.4: the "word-split" model was wrong — the quoting broke all six hooks on real Grok Build. See the 1.4.4 entry.)_

## [1.3.9] — 2026-07

### Fixed
- Antigravity (agy) judge invocation: agy 1.1.x changed `--print` to take the prompt as its value; the panel's agy seat had been silently reviewing the wrong prompt.

## [1.3.8] — 2026-07

### Added
- Judge-pin maintenance loop: `tasks models check` (live availability audit with probe-confirmed hard stops when a pinned model is gone) and `tasks models select` (guided panel refresh); `tasks doctor` warns on dead pins.

### Fixed
- Failed judges (budget-exhausted, nonzero-exit) can no longer masquerade as successful empty reviews.

## [1.3.7] — 2026-07

### Added
- Per-install review knobs in `.agent/config.json`: `judge_budget_usd`, `review_timeout_secs` (CLI flag → env → config → default precedence).

## Earlier

Wrapper resolution and atomicity hardening (1.3.5–1.3.6), Codex `apply_patch` hooks + freehand mode (1.2.x), monitor integration (1.1.6), retro tooling, bash-log, mind-map subsystem, marketplace packaging (1.0.x). See git history for detail.
