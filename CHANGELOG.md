# Changelog

Notable changes to the playbook plugin. Follows [Keep a Changelog](https://keepachangelog.com/) loosely; maintained by the README audit skill (entries before 1.4.2 are reconstructed from git history and the project mind map).

## [1.4.5] — 2026-07-27

Three efforts ship together in this release: the merge skill's genericization (task 021), the completion of per-user lane support across every plugin surface (task 022), and the DEBUG-trap fix that restored gate logging (task 023).

### Fixed (task 023 — gate logging silently stopped)

- **Command logging was killing every `set -e` hook, so gate logging stopped entirely.** `bash-log.sh` is sourced into every non-interactive bash through `BASH_ENV`, where it installs a `DEBUG` trap. Four of the trap's noise-filter arms exited with a bare `return`, which inside a DEBUG trap re-emits the *stale* `$?` of the previously executed command — and a DEBUG trap returning non-zero terminates a `set -e` shell. `state-echo-hook` therefore died at its first false conditional, before writing `gate_key` or any `**[G…]**` entry: no error, no output, no gate log, indefinitely. The arms match exactly the commands hooks run constantly (`[ -d …`, `[[ …`), so the failure was immediate and total on any host where the bash logger was deployed. Every exit path of `_cpb_log_cmd` is now explicitly `return 0`.

  Reported from the field with the root cause already isolated and the fix proven (Cristi / ai-ring-vet, 2026-07-21; gate logging dead on that install since 2026-07-01, the day a `playbook init` first deployed the logger). Reproduced here on bash 3.2 and 5.2 — it is not shell-version specific.
- **An unwritable `bash_history` no longer takes the shell down with it, or floods hook output.** A failing append is itself a failing command inside the trap, so it killed `set -e` hosts by the same mechanism; it is now guarded. The guard uses a brace group (`{ echo …; } 2>/dev/null || return 0`) because `echo … >> file 2>/dev/null` does **not** suppress a failure to *open* the file — bash reports that before applying the redirect — which, once per command, meant one error line per command in every hook's output.

### Changed
- **The merge skill no longer hardcodes one project's layout or test runner** (field report, AloVet 2026-07-24; task 021). Step 7's verification bundle assumed a `backend/` directory and a `run-backend-tests` command, which contradicted the skill's own claim to need only `.agent/` and the two mind-map files. On a repo without them the code-identity check diffed a directory that didn't exist — reporting green for having examined nothing — and the test step errored outright. Both are now repo-agnostic:
  - **Code identity** diffs the whole tree minus the paths the semantic steps own (`git diff "$target_before" -- . ':(exclude)MIND_MAP.md' ':(exclude)MIND_MAP_OVERFLOW.md' ':(exclude).agent'`). This covers code wherever it lives, including root-level files like `package.json` or `main.py` that a configured directory list would miss, and an empty result now truthfully means "the merge introduced no code."
  - **Project soundness** runs the command the project declares in `.agent/config.json` as `{"merge_verify": {"command": "…"}}`, via the new bundled `merge-verify.py`. Nothing is inferred: declare no command and the skill says so instead of guessing one.
- **`.agent/config.json` is now documented as committable**, with two tiers of ownership: review knobs (`judge_budget_usd`, `review_timeout_secs`) stay per-install and overridable through `PLAYBOOK_*` env vars, while `merge_verify` is project policy that only works when every clone sees it. `merge-doctor` treats a tracked `config.json` as correct rather than legacy detritus — previously it demanded `git rm --cached` on it, which would have failed the merge skill's own Step 7(b) gate for any repo that followed the new instruction to commit the file.

### Removed

- **`playbook-gemini`**, superseded by `playbook-agy` after the CLI's rename. It was unreachable from any initialized project — absent from both `init`'s wrapper generation and the wrapper-healing registry — and still exec'd the sunset `gemini` binary.

### Added
- `tests/test_provider_multiuser.py` (34 tests) and `tests/wrapper-multiuser-fixture.sh` (165 assertions). Both layouts, every invalid-marker branch, the real hook subprocess exit codes, and a split-brain end-to-end that runs the launcher, the `tasks` CLI and the Codex hook against one scratch repo to prove all three land in the same lane. A shared vector table asserts `provider/paths.py`, `tasks/core.py`, `gate-echo-lib.sh` and `monitor-nudge.sh`'s inlined copy classify markers identically, so the resolvers can't drift apart.
- **`skills/merge/merge-verify.py`** (pure stdlib, ships with the skill). Runs the declared command via a temp script so quoting is preserved (`pytest -k 'not slow'` keeps its meaning), and reports a four-way verdict through its exit code: **0** GREEN, **1** FAILED (the command's own rc appears in the status line), **2** BLOCKED (declared but unusable), **3** SKIPPED (nothing declared). `--plan` classifies without running; `-C` sets the project root.
- `tasks doctor` now warns on an unusable or empty `merge_verify`, using the same rules the merge enforces (doctor loads the skill's resolver rather than keeping a second copy that could drift). It also warns when a `merge_verify` is declared in a `.agent/config.json` that git isn't tracking — the gate would then exist in one clone only — and says explicitly that a malformed config makes the merge **block** rather than fall back to defaults.

### Fixed

- **Multi-user repos: the surfaces that provision session state were writing to the wrong lane.** On a repo with a `.agent/current_user` marker, the `tasks` CLI and the bash hooks correctly used `.agent/<user>/`, but the provider launchers, the provider Python layer, the Codex hooks, the monitor, the shell loggers and `init` all still read and wrote the shared root `.agent/`. The visible symptom was **gate enforcement silently failing under Codex** — `tasks work <N>` recorded the active task in the user's lane while the hook looked for it at the root, so every edit was treated as "no active task". Now lane-resolved end to end:
  - `playbook-{codex,agy,grok}` no longer carry their own root-only project-root walk; all four launchers (pi included) use the shared resolver and provision `<lane>/sessions/<id>/`.
  - New `provider/paths.py` backs `provider/adapter.py` and `provider/codex_hooks.py` (session state, task lookup, chat log, counters, writability). It raises a catchable `InvalidUserMarkerError` rather than `SystemExit`, so the Codex hooks' per-event policy still applies — a malformed marker denies through the normal channel instead of bypassing it.
  - The three `codex-*-hook` scripts' fallback project-root walk now recognizes the multi-user layout.
  - The monitor is lane-aware throughout: session discovery, `MONITOR_DIR`, the sandbox writable scope, the briefing (including the task/chat-log globs inside `bootstrap.sh`'s embedded Python), and **`monitor-nudge.sh`**, which read nudges from the root while the monitor wrote them to the lane — every nudge was silently undelivered.
  - `bash-log.sh` / `bash-log.zsh` append to the lane's `bash_history` — the file `tasks retro` and `tasks context` actually read — using only shell builtins, since they run per command.
  - `init` provisions `tasks/`, `playbooks/` and `monitor/` into the lane. `config.json` and `models.json` stay at the `.agent/` root by design (shared repo policy; per-clone judge pins).
- **Fresh clones of a multi-user repo now fail loud instead of creating a phantom lane.** `.agent/current_user` is gitignored, so a clone arrives with lanes and no marker; defaulting to the root would quietly create a second, competing state tree. `init` and the launchers stop with the one-line fix. A repo that legitimately has both a root `.agent/tasks/` and per-user lanes is unaffected.
- **No surface falls back to shared root state on a malformed marker.** State-creating surfaces refuse; the shell loggers and the nudge hook skip silently rather than take down a live shell or a tool call.
- `launch-monitor` printed nothing at all when the sessions directory didn't exist yet — `return 1` under `set -e` killed the script before its own "no main agent running" message. It now explains itself.

### Fixed (task 022 impl review)
- **A malformed `.agent/current_user` no longer resolves to the shared root anywhere — including reads.** The provider adapters degraded a bad marker to the root lane; besides writing `chat_log_offset` there, that let a *stale root task* surface as the active task, so leftover state in an unrelated lane could satisfy the edit gate. Adapters now report no active task and skip writes.
- **Every state-creating surface now honors the fresh-clone guard, not just `init` and the launchers.** `tasks new`, `tasks init`, the state-writing hooks, the Codex hook write paths and the monitor all previously created root state on a clone with lanes but no marker — and because a root `.agent/tasks/` is itself a valid lane, a single `tasks new` permanently converted the guarded shape into an "allowed mixed layout", disarming the guard everywhere else. Direct invocations fail loud; hooks skip quietly (a hook that aborts takes the session down) and `session-start-hook` warns; enforcement still fails closed.
- **`codex-stop-hook` and `codex-user-prompt-hook` had no exception handler at all**, so a malformed marker exited 1 with a traceback rather than the per-event policy the design documents. Both now fail open, which is correct for hooks that cannot block.
- **The Codex path helpers no longer create directories.** `_turn_baseline_file` and `_stop_block_marker_file` called `mkdir` inside what were nominally path lookups, so merely *reading* a baseline conjured a session dir — on a fresh clone, a phantom lane created by a function that only meant to look.
- **One marker-parsing contract across every implementation.** The shell readers took only the first line and kept a trailing `\r`, so `alice\n../evil` resolved to lane `alice` where Python rejected it, and a CRLF marker silently disabled command logging and monitor nudges. The marker is now exactly one line, CR-stripped, whitespace-insensitive, with a missing final newline tolerated — matching Python, which needed no change.
- `bash-log.sh` lane resolution guards its `read`: a marker without a trailing newline could trip `errexit`.
- **The monitor's briefing missed its transcript on any path containing a space** (`$SLUG` unquoted in `bootstrap.sh`) — iCloud Drive's "Mobile Documents" is the common case. The monitor's own instructions (`CLAUDE.md`, mind-map stub) also still named root paths, so its manual and fallback writes targeted the wrong lane.
- Generated judge prompts gated the user's original messages on a root `.agent/chat_log.md`, telling judges to skip them on multi-user repos.

### Fixed (task 021 impl review)
- **A failing early step no longer reports GREEN.** The declared command now runs under `set -e -o pipefail`. Bash reports only the last command's status, so `typecheck && test` written across two lines — or anything ending in a pipe — could exit 0 with a red step behind it. That was the same "green stamp on an unexamined tree" defect this release set out to remove, reintroduced inside its own replacement.
- **A present-but-unreadable `.agent/config.json` now BLOCKS instead of reporting SKIPPED.** Only `FileNotFoundError` counts as "nothing declared"; permission errors and a directory in its place are surfaced, because a policy file that exists has been declared and calling it absent is a false statement.
- **`--plan` no longer returns exit 0.** It reports a distinct `4 CONFIGURED`, so a classification probe (which runs nothing) can never be mistaken for the one code the push gate accepts.
- **The code-identity pathspecs are root-anchored** (`':/'`, `':(exclude,top)…'`). The cwd-relative form silently narrowed the check to a subtree when run from a subdirectory, and still exited 0 — a partial scope wearing a whole-tree result.
- `tasks doctor` no longer crashes when the shipped runner is corrupt (the module load moved inside the advisory's `try`), the Step 4 background note uses a portable `mktemp` template (`mktemp -t name` is BSD-only and errors on Linux) and prints the log path a later shell can't otherwise know, and `docs/configuration.md` no longer claims the command runs *after* the merge commit — it runs on the merged tree before it.

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
