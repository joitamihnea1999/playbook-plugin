# Changelog

Notable changes to the playbook plugin. Follows [Keep a Changelog](https://keepachangelog.com/) loosely; maintained by the README audit skill (entries before 1.4.2 are reconstructed from git history and the project mind map).

## [1.5.5] — 2026-08-13

The field-backlog release: every item below carries evidence from the StrataDB stress test (batches 1–4) or the 1.5.4 full-surface gauntlet — see the fork owner's lab notebook.

### Verified

- **The close-time tree-state freshness advisory fires** (F17 — flagged unverifiable in the field because it is console-only). Reproduced end-to-end under the field scenario's exact conditions (impl round stamped, code edited post-panel, close): the advisory prints; a matching fingerprint stays silent. The 1.5.3 suite never exercised the close path — `tests/test_freshness_advisory.py` now pins mismatch-fires, match-silent, and no-impl-round-silent.

### Added

- **`init` writes CLAUDE.md and .gitignore mechanically** (F15 — gauntlet: those files were the AGENT half of /playbook:init, so the doctrine they carry held only if the agent performed it; it did, but a guarantee beats a habit). New `scripts/claude-md-merge.py`, run by `scripts/init`: CLAUDE.md is created from the template, or **merged, never clobbered** — template-owned sections update in place, everything else (a seeded pointer, custom sections, the project title) survives byte-for-byte, re-runs are idempotent; .gitignore gains one marker-guarded block of playbook runtime-state entries (sessions, chat log + counters, bash history, `current_user`, `models.json` — root and per-user lanes both), appended exactly once, existing content untouched. `/playbook:init`'s agent step shrinks to reviewing the merge and adding what the template cannot know. Negative control in the tests: a seeded CLAUDE.md pointer survives a re-init.
- **Standing gates** (F8 — the journal gate was hand-relocated below Pre-review VERBATIM on two consecutive field tasks; a gate a project wants on every task should come from generation, not agent memory). `standing_gates` in `.agent/config.json`: a list of `{title, text}` entries appended in declared order as the **final gates** of every generated task — base templates, quick, custom playbooks, and stub expansion alike. `{{NNN}}` substitutes the task number (`journal/{{NNN}}.md`). Opt-in: absent means generation is byte-identical to before, and `init` seeds none. Malformed entries and title collisions are skipped loudly; title/text are collapsed to one line so config can never mint a phantom section or gate (the #09 disease, closed at the config door too). Documented in `docs/configuration.md`.

### Fixed

- **Chat attribution can no longer go blind on the messages that matter most** (F2 — the judges' intent-check lens ran with no data on the first real panels). Three legs, one class: (1) the earliest-activated task's attribution window now opens at the epoch, not at its activation — the message that DEFINES a project (the owner's mandate) predates `tasks work 1` by construction and was unattributable to anything; (2) `tasks tag` applies the same pre-history rule when writing `<!-- TNNN -->` spans; (3) `tasks context` falls back to timestamp-window attribution (gate entries + bash_history — the same fallback `tasks intent`'s chat layer already had) when no spans exist, because nothing runs `tasks tag` automatically, so the span-only reader was blind for EVERY task on real projects. Verified against the real field data: task 001 now returns the mandate, task 010 returns the owner's "go with transactions" decision. Still fails loudly when nothing is attributable, and the provenance note goes to stderr — stdout stays pure messages. Also: `extract_chatlog` no longer leaks the `(provider/pid)` header suffix into message text (same disease `tasks log` had, fixed in 1.4.3; the extractor was missed).
- **`tasks doctor` no longer cries wolf about an install it isn't running from** (F16 — batch-4: 4 FAIL / 8 WARN while every hook demonstrably enforced all session). The hook-presence, gate-truncation and session-id-resolver checks now resolve the RUNNING code's own `scripts/` dir first — the same tree the version check reads — instead of hunting `~/.claude/plugins` by mtime and inspecting whatever cache sorted newest; the home glob survives only as a last resort for dev layouts. And `hooks_check_report` findings in copies other than the authoritative one (CLAUDE_PLUGIN_ROOT, else the running tree) are now labelled "other install copy — not the one this CLI runs from": the cross-host scan stays (a stale grok copy WAS the firing one in the AloVet bug), but stray-cache noise no longer reads like a defect in the live install. A defect in the bound copy still warns at full volume (negative control in the tests). Doctor reports only — no runtime resolution or enforcement changed.
- **`reversible` now asks whether the WORLD reverts, not the diff** (F11 — third judge-driven reclassification in the field). The template defined `reversible` as "`git revert` undoes it completely", which reads as *diff*-revertibility: an agent classified data-loss-class work `reversible` reasoning "the DIFF is git-revertible" while its own notes said the blast radius was data-loss-class. Both teaching sites (the `## Risk` block in every rendered task, and the CLAUDE.md `init` seeds) now state the operative question — persisted data, on-disk formats, secrets, history, and published claims never qualify even when the diff reverts cleanly.

## [1.5.4] — 2026-08-13

Three defects found by the full-surface live gauntlet (a scratch project driven through every command of the INSTALLED plugin, ending in real codex+grok panel runs). Each fix carries a regression test.

### Fixed

- **`extract_parked_items` read only the FIRST `## Parked` section.** The template ships one, so a second section (agent-added, or produced by receipt reordering) was invisible to `tasks parked` and the close-time surface — parked debt silently unswallowable again. All sections are read now. The multi-heading hazard, same family as #09.
- **`resolve_agent_dir` crashed on a `str` project path** — the single chokepoint every state helper funnels through now coerces, closing the whole class (third instance of the str/Path disease; the first two were in audit).
- **The single-judge fallback could not write findings into panel-first tasks.** 1.5.2 renamed the section placeholder to "…triage appears here"; the fallback write-back still anchored only on the old "…findings appear here", so a live judge's findings refused to land (loudly and safely — but the fallback exists precisely for degraded days). Both placeholder generations anchor now.

Gauntlet outcomes worth recording: init's mechanical half, full task lifecycle, close-gate matrix (verify pass/fail/timeout, force+reason, dirty markers, receipts upsert), blocked state through the real Stop hook, audit sweeps incl. mind-map staleness and task-bloat, retro trigger at 10, merge-verify's five exit codes, quorum verdicts, per-transport context receipts, tree-state freshness advisory, and two REAL cross-provider panels (codex stdin + grok argv, 2/2 PASS, rounds stacked, close accepted against real evidence) — all verified working.

## [1.5.3] — 2026-08-13

Context perfection + judge execution (L1), red-teamed before building: the design review of our own plan found seven flaws — including one in already-shipped code and a sandbox write hole — and every fix below carries the correction.

### Fixed

- **judge.md stacks rounds instead of clobbering them** (newest first, retention 5 with a loud trim note; legacy/unparseable content preserved as an opaque block). A re-run panel no longer destroys the previous round's verdicts.
- **Panel evidence is parsed structurally, not by substring.** The close gate reads the NEWEST round's mode+verdict only — a stale impl-PASS buried under a newer FAIL, or under a newer plan round (which implies replanning), no longer satisfies `panel_required_for`. The substring version shipped in 1.5.2 and was correct only by accident of the old clobbering behavior.
- **Sandbox bind order** (shipped earlier on this branch): broad rw mounts (/tmp, home subpaths) now bind BEFORE the project, so a /tmp-resident project can no longer be re-exposed writable to judges. Found by the empirical spike that validated judge execution.

### Added

- **Per-transport context budgets.** stdin seats (claude, codex — no OS argv limit) receive up to `review_context_chars_stdin` (default 200k chars); argv seats (grok/agy/pi) keep `review_context_chars` (default 100k) under the byte guard. judge.md's Context receipt reports per transport with seat names; a TRIMMED seat's own prompt now says exactly which sections were dropped and where the full task.md lives (it has repo read access — an elision is an instruction to go read).
- **Tree-state fingerprints.** Panels stamp `**Tree-state:**` (sha256 over HEAD + status + diff, `.agent/` excluded so triage edits don't false-positive); the close prints an advisory when the newest impl round's fingerprint no longer matches the code. Content, never mtimes.
- **Judge execution, level 1** — `judge_verify` in `.agent/config.json` declares commands safe to run inside the judge's read-only sandbox (empirically verified: exec and /tmp writes work, repo writes blocked). Prompt rules keep execution evidence honest: hypothesis-first, reproduce-twice, no timing evidence (parallel judges contend), targeted use only. Undeclared projects get exactly the old prompt.
- **Pinned sections** — `<!-- pin -->` on its own line under a task.md heading forces that section through any context trim (the heuristic can't know which old decision is load-bearing; the author can). Deliberately NOT in the heading text, which is parsed exactly by the receipts/evidence family. Over-budget pins hard-truncate with a loud receipt.
- **Sanctioned compaction + task-bloat sweep.** The task sticker now carves out the one exception to "never replace original text": old review-round narrative may move VERBATIM to `task-archive.md` (never gates, never Intent/Design/Parked/receipts). `tasks audit` gains an advisory `task-bloat` check that nudges compaction when an OPEN task.md outgrows the review budget.
- Single-judge (fallback) path: transport-aware budget, trim notice, judge_verify clause, and a durable `[context]` receipt line at the top of its log.

## [1.5.2] — 2026-08-13

Panel-always, by owner decree from the field test: "another pair of eyes is always better, so why not enforce this?"

### Added

- **`panel_required_for` policy** (`.agent/config.json`: `"all"` or a list of risk classes). For closes in scope, the evidence bar becomes **panel-grade**: a `judge.md` whose impl panel reached quorum (`PANEL VERDICT: PASS`). A plan-mode panel does not count (it cannot vouch for what was built) and a FAIL-verdict panel does not count (a degraded panel is not a panel). `--force --reason` remains the recorded escape hatch. `init` seeds new projects with `"panel_required_for": "all"`.
- **Templates are panel-first.** The mandatory Plan Review and Implementation Review gates now run `tasks panel-review` (all available judges in parallel, findings in judge.md led by a verdict), with single-judge `plan-review`/`impl-review` demoted to documented fallbacks. Triage guidance now asks the agent to name which judge it believed when judges disagree — disagreement between models is signal.

### Changed

- Shipped default panel is provider-diverse: `opus, sonnet, codex:gpt-5.5, grok, agy` (grok seated, third same-family claude seat dropped). Availability filtering still skips CLIs that are not installed.

## [1.5.1] — 2026-08-12

First fixes driven by the StrataDB field test (five real tasks, one session handoff — findings in the fork owner's lab notebook).

### Fixed

- **Dirty-close honesty.** The normal flow closes a task and commits after, so a Verification Receipt's `commit X` named a commit that did not contain the verified code — observed live with a closed task whose entire work sat uncommitted at session end, one crash away from silent loss. `tasks work done` now records `commit X (+N uncommitted file(s) — verified code is NOT in this commit)` in the receipt and warns out loud to commit before ending the session.
- **Mind-map staleness detector: three field false-positives and one crash.** Extension alternation now matches longest-first with a boundary guard (`config.json` no longer reads as `config.js`); all-caps-stem placeholders (`journal/NNN.md`) are naming-scheme documentation, not stale citations; citations into walker-excluded dirs (`.agent/…`) are unjudgeable and skipped, with `./`-prefix removal no longer mangling dot-dirs; a `str` project path no longer crashes the check. A noisy detector gets ignored — these keep it quiet unless it is right.

## [1.5.0] — 2026-08-11

The correctness-contract release: a close is now **earned, not asserted**, and the information judges and agents reason over is kept complete, current, and honest. Driven by a 79-task field report (13 design gaps) plus the `upstream-issues/` defect write-ups; every mechanism below ships with tests, including negative controls proving each new detector can report failure (suite 514 → 640).

### Added

- **Evidence contract at close** — `tasks work done` runs the project's declared `verify` commands (`.agent/config.json`, per-risk-class with an `_always` base bar; legacy `merge_verify.command` honored as fallback), records a **Verification Receipt** (command, exit code, output head, commit, timestamp) into task.md, and refuses to close on a failing verify. `--force` now **requires `--reason`**, stored in the receipt. Verify commands run under a hard ceiling (`verify_timeout_secs`, default 1200s) — a hung suite fails the close instead of hanging it.
- **`## Risk` classification** (`reversible` / `irreversible` / `assertive`), set at the Structure gate. Assertive (changes a claim about the world) and irreversible tasks cannot close without **implementation-grade** review evidence — a plan-phase review does not vouch for what was built, and a small diff is not a review waiver.
- **`tasks audit [<N>]`** — mechanical pre-review sweeps under the grep exit-code convention (0 findings / 1 clean / ≥2 **error, never a pass**): conflict markers, merge artifacts (`.orig`/`.rej`), stale markers, plus a built-in **mind-map staleness check** (cited paths that no longer exist in the tree; `audit.mindmap_severity: "error"` for zero tolerance). Receipts land in task.md; reviews warn when the audit is missing **or stale** (receipt commit ≠ HEAD). Project sweeps via `audit.sweeps`, per-sweep timeout via `audit.timeout_secs`.
- **Panel verdict** — panel reviews resolve a quorum (`panel_quorum`: int, fraction, `majority` default, `all`), lead judge.md with **PANEL VERDICT: PASS/FAIL**, stamp the reviewed commit, and exit non-zero below quorum. A 1/7 and a 7/7 panel are no longer the same exit code.
- **`tasks blocked "<reason>"`** — an honest state for "paused awaiting the user's decision": satisfies the Stop hook without a fabricated checkbox, records the reason, shows as BLOCKED in `list`/`status`, skipped by active-task discovery, resumed by `tasks work <N>`.
- **`tasks parked [--all]`** + close-time surfacing of open parked items (`[promoted → N]` / `[dismissed: reason]` lifecycle), and a close-time **retro nudge** once 10+ tasks closed since the last retro.
- **Context fidelity for reviews** — task.md context is selected by structure (Intent/Design/Handoff always, most-recent sections next) and **every truncation is receipted** in judge.md and stderr; the old head-slice silently dropped the newest rounds first. POSIX per-element **argv byte guard** (`MAX_ARG_STRLEN`) for grok/agy/pi seats, refusing loudly pre-dispatch instead of a cryptic `E2BIG`.
- Review prompts now require a **`CAP:` line** (cap-bound vs exhausted) so "no new findings" is readable as convergence vs saturation, and the panel triage frame names what review **cannot** catch (correspondence, disclosure, irreversibility) with the real check for each.

### Changed

- Gate parsing is line-anchored and shared — a `- [ ]` in prose is no longer a gate, and count, head-position, and Stop-hook verdicts are held equal by a parity test (a task could previously close at 71/74 while `status` said "all gates checked"). `## Status` reads/writes agree on last-heading-wins across Python and the Stop hook.
- Receipts **upsert** under one heading (newest entry first) instead of appending duplicate sections on re-close/re-audit; task-state writers go through an atomic temp+rename write.
- `/playbook:init`'s CLAUDE.md template now teaches the correctness contract (risk classes, verify contract, audit, blocked, judge-triage discipline) instead of CLI mechanics alone.

### Removed

- `tasks global-retro-collect` (cross-project retro collection). This fork's model is init-per-project with no cross-project knowledge; the multi-user lane helper it hosted moved to `tasks/core.py` for `tasks doctor`.

## [1.4.7] — 2026-07-29

A field-reported bug that silently revoked the agent's permission to edit code mid-task, the recovery dead-end it led to, and task 023's two deferred `init` fixes (task 027).

### Fixed (session pointer loss — the headline)

- **`SessionStart` no longer deletes the live session's own state directory.** The GC sweep in `scripts/session-start-hook` decided whether a session was dead purely from the mtime of its `current_state` file — with no liveness check and no self-exclusion, so it could (and did) `rm -rf` the directory it had created twelve lines earlier. That mtime was never a liveness signal: `current_state` is written **only** by `tasks work <N>`, so it records when the task was *activated* and is never refreshed by activity. Any task active for more than 24 hours was therefore **guaranteed** to lose its pointer at the next `SessionStart` — and because the hook is registered with no matcher, that fires on `compact` too, so the longest, busiest sessions re-rolled the dice at every compaction. The consequence was not cosmetic: with the pointer gone, `task-gate-hook` hard-blocks `Edit`/`Write` with "BLOCKED: No active task", so a live mid-task session loses the ability to edit source with nothing connecting it to a GC sweep. The sweep now mirrors `tasks/cli.py::_gc_dead_sessions` exactly — never its own session, `pid-*` kept by `kill -0` liveness alone, 24h mtime only for legacy non-PID names — with every path quoted (the old `find -exec dirname | xargs rm -rf` also word-split on spaces, so on any `Mobile Documents`-style path it both missed its target and aimed `rm -rf` at path fragments) and every removal fail-open, so an undeletable directory cannot abort session start.
- **One policy, not two.** The Python GC had always done this correctly, so the two implementations enforced contradictory policies over the same directory — and the bash one ran first. The policy now lives in a single predicate, `_session_is_dead`, shared by `_gc_dead_sessions` (which deletes) and `tasks doctor` (which reports), with the bash sweep as its documented twin and a test that runs one synthetic tree through both sweepers and compares the results.
- **`/clear` no longer deactivates the active task.** `SessionEnd` is also registered with no matcher, and on `clear` the same process keeps running — so its unconditional `rm -rf` of the session directory dropped the pointer mid-session, reaching the same "No active task" failure by a second path. Cleanup now skips `reason=clear`, and still runs for every reason that means the process is going away.
- **`tasks doctor` stopped reporting healthy sessions as stale.** It applied the same mtime-only rule with no liveness check and no self-exclusion, so after the fixes above its entire output would have been the false-positive class this release removes: a live session on a multi-day task is the normal case, not a fault.
- **Self-exclusion no longer depends on env propagation.** It read `PLAYBOOK_SESSION_ID` and fell back to `""` when unset; on Windows, where `resolve_session_id()` returns the constant `pid-win-fallback`, `int("win-fallback")` then raised and the shared session directory was deleted at *every* CLI invocation. It now falls back to `resolve_session_id()`.

### Fixed (recovery)

- **`tasks work <N>` can now re-adopt a finished-but-unclosed task.** The state the field report ended in — all gates checked, `## Status` still `pending`, no session pointer — was unreachable through the CLI: `work done` reads the pointer (absent → "No active task", and it never touches `## Status`), while `work <N>` refused the task because `_find_active_task` only returns tasks with open gates and the fallback only re-activated a `done` task or a stub. The only sanctioned writer of `## Status` needed a pointer, and the only way to get a pointer was refused, so the pointer had to be hand-written. A third fallback arm now re-adopts such a task and points you at `tasks work done`; it deliberately does **not** rewrite `## Status`, keeping `work done` the only thing that closes a task.

### Fixed (task 023's deferred `init` items)

- **`bash-log.sh` is now deployed on every host, so `BASH_ENV` stops dangling.** `init`'s `$SHELL` branch deployed only `bash-log.zsh` on zsh hosts, while the `settings.json` injection always set `BASH_ENV=~/.claude/bash-log.sh` — so on every macOS/zsh install that variable pointed at a file that was never written, and Claude Code's bash-side command logging was silently dead. The `$SHELL` branch now decides only the *extra* host-shell integration (zsh additionally gets `bash-log.zsh` and a `.zshenv` source line, since zsh ignores `BASH_ENV`). Task 023 found this and deferred it deliberately, because fixing it arms bash-log's `DEBUG` trap on every zsh host — unsafe until the trap's `set -e` kill path was fixed, which shipped in 1.4.6. Existing broken installs heal on the next `/playbook:init` or `/playbook:upgrade`.
- **Deployed `bash-log` copies are CRLF-normalized.** They are sourced into every hook shell, so a CRLF copy from a Windows checkout breaks them — and byte-comparing a CRLF source against an LF destination never matched, so `init` re-copied on every run and never reported "unchanged". Deployment now normalizes through a temp file and an atomic rename, and compares against the normalized bytes.

### Tests

- New `tests/init-bash-log-fixture.sh` (27 assertions): both host types, `BASH_ENV` asserted to name a file that *exists*, idempotency across two runs, CRLF normalization, and a negative control that rebuilds the pre-fix either/or deployment and confirms it dangles.
- New `tests/test_session_gc_policy.py` (11) and `tests/test_work_readopt.py` (13); new `S18` in `tests/wrapper-multiuser-fixture.sh` (27) covering the sweep, bash/python parity, `SessionEnd` reasons, and three negative controls.
- Fixture-hygiene fix: `S14`'s `init` runs used the developer's **real** `$HOME`, so every suite run mutated `~/.claude/`, `~/.claude/settings.json` and a shell rc file on the machine under test. They now run under an isolated temporary `HOME`.

## [1.4.6] — 2026-07-29

Two field-reported bugs and the workflow improvements that came with them, all from Cristi (ai-ring-vet) on Windows 11 / Git Bash MSYS: gate logging had stopped silently (task 023) and wrapper regeneration was truncating (task 024).

**Why this is 1.4.6 and not part of 1.4.5:** 1.4.5 was never tagged, so these fixes were originally folded into it. That was wrong — the marketplace serves `main`, and 1.4.5 had already been on `main` for the tasks 021+022 work, so one version number would have labelled two materially different code states. Anyone who installed 1.4.5 before this has a plugin whose version matches what the repository says while missing the gate-logging fix, and nothing can tell them: `tasks doctor`'s version check compares the manifest and the source *inside one tree*, so it reports a match either way and never looks at what `main` now holds. The version number is the only signal that reaches you. If you are on 1.4.5, upgrade.

### Fixed (task 024 — Windows wrapper generation)

- **On Git Bash / MSYS, every regenerated wrapper was silently truncated.** `create_wrapper` builds `.claude/bin/<name>` from a heredoc whose delimiter was `WRAPPER`, while the template's own body contains a line starting `WRAPPER_DIR=`. MSYS bash 5.2's `$( )` parser terminates a heredoc at a body line that merely *starts with* the delimiter, so the wrapper was cut to its first six lines. `session-start-hook` regenerates `tasks`, `sandbox` and all four `playbook-*` wrappers on every session start, so on Windows this rewrote `.claude/bin/tasks` — the CLI that arms the gate hook — as a stub, every session. The delimiter is now `END_WRAPPER_TEMPLATE`.

  It does not reproduce on a permissive parser (macOS bash 3.2 captures the body either way), so `tests/test_wrapper_template.py` pins the structural invariant instead: no line of a heredoc body may start with that heredoc's own delimiter, checked across every shipped shell script, for quoted and unquoted forms alike, with terminator matching that follows bash (column zero; tabs stripped only for `<<-`). It also asserts a generated wrapper is *complete* — the existing atomicity fixture only checked that the file was non-empty, which a six-line stub satisfies.

### Changed (task 024 — contributed workflow improvements)

- **The mind-map pre-review gate now names the rule instead of a vague aspiration.** It was "MIND_MAP.md updated if new insights emerged", which invited one new node per task; it now asks for the OWNING subsystem node to be updated in place, with a new node only for a genuinely new subsystem. Applies to every task type with a pre-review section (`quick` has none by design).
- **Judge prompts gained a hostile-sequence lens.** Plan and implementation reviews — single-judge and panel — now walk, for every state-changing flow: two concurrent requests, the same logical event delivered twice under distinct ids, reordered events, an external call succeeding while the local transaction rolls back, a crash after commit but before any post-commit step, and a retry after a lost response. Reviews state the invariant and the test that proves it, or raise a finding. Changes that touch no shared or persisted state say so in one line, so the lens stays sharp instead of manufacturing findings.

  Both of these were contributed by **Cristi (ai-ring-vet)**, who also diagnosed the DEBUG-trap failure in task 023 — the hostile-sequence lens comes from a payments-heavy backend where it has caught real defects.

### Fixed (task 023 — gate logging silently stopped)

- **Command logging was killing every `set -e` hook, so gate logging stopped entirely.** `bash-log.sh` is sourced into every non-interactive bash through `BASH_ENV`, where it installs a `DEBUG` trap. Four of the trap's noise-filter arms exited with a bare `return`, which inside a DEBUG trap re-emits the *stale* `$?` of the previously executed command — and a DEBUG trap returning non-zero terminates a `set -e` shell. `state-echo-hook` therefore died at its first false conditional, before writing `gate_key` or any `**[G…]**` entry: no error, no output, no gate log, indefinitely. The arms match exactly the commands hooks run constantly (`[ -d …`, `[[ …`), so the failure was immediate and total on any host where the bash logger was deployed. Every exit path of `_cpb_log_cmd` is now explicitly `return 0`.

  Reported from the field with the root cause already isolated and the fix proven (Cristi / ai-ring-vet, 2026-07-21; gate logging dead on that install since 2026-07-01, the day a `playbook init` first deployed the logger). Reproduced here on bash 3.2 and 5.2 — it is not shell-version specific.
- **An unwritable `bash_history` no longer takes the shell down with it, or floods hook output.** A failing append is itself a failing command inside the trap, so it killed `set -e` hosts by the same mechanism; it is now guarded. The guard uses a brace group (`{ echo …; } 2>/dev/null || return 0`) because `echo … >> file 2>/dev/null` does **not** suppress a failure to *open* the file — bash reports that before applying the redirect — which, once per command, meant one error line per command in every hook's output.

### Documentation

- The upgrade caveat in `docs/architecture.md` under-warned: it said a stale `~/.claude/bash-log.{sh,zsh}` copy costs you monitor nudges and lane-correct shell history, omitting that a copy from before this release silently disables gate logging entirely. `docs/cli.md` documented the review commands without saying what a review checks, which left the new hostile-sequence lens invisible. Both fixed, and the README now names the shell on Windows (Git Bash / MSYS).

## [1.4.5] — 2026-07-27

Two efforts ship together in this release: the merge skill's genericization (task 021) and the completion of per-user lane support across every plugin surface (task 022).

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
