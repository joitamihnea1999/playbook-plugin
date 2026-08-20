# Changelog

Notable changes to the playbook plugin. Follows [Keep a Changelog](https://keepachangelog.com/) loosely; maintained by the README audit skill (entries before 1.4.2 are reconstructed from git history and the project mind map).

## [1.5.34] — 2026-08-19

A single Critical correction, scoped narrowly: on a fresh clone of a multi-user
repo the bundled shell logger wrote the shared root history file instead of
skipping. Nothing else in the product changes.

### Fixed

- **The shell logger elected the shared root lane when it could not know the
  owner.** `.agent/current_user` is gitignored install-local, so a clone of a
  multi-user repo arrives with `.agent/<user>/` lanes and no marker. Both
  bundled loggers initialised the lane to `"$dir/.agent"` and only reassigned it
  inside the `if [[ -f current_user ]]` branch, so the marker-absent path fell
  straight through: every command appended to the SHARED root
  `.agent/bash_history` — the cross-user contamination the lane model exists to
  prevent — and the user's own lane history was never written. Once the user
  then applied the documented fix (`echo <user> > .agent/current_user`), the
  resolved lane moved and everything logged beforehand was orphaned.

  The lane now starts unknown, and the decision rests on exactly two filesystem
  facts — whether `.agent/current_user` is present and valid, and whether root
  `.agent/tasks/` is a directory:

  | shape | lane |
  |---|---|
  | valid marker | the validated user lane |
  | no marker, root `.agent/tasks/` exists | the root IS a legitimate lane |
  | anything else | owner unknown — skip logging |

  Nothing about the children of `.agent/` is consulted. That makes the loggers
  **deliberately stricter** than `provider/paths.py::lanes_without_marker`,
  `tasks/core.py` and `gate-echo-lib.sh`, rather than in parity with them: on a
  marker-absent project with no root `tasks/`, those three still answer the
  root and the loggers write nothing. See the next bullet for what that costs.

  A **dot-named** lane directory (`.agent/.hidden/tasks/`) is no longer part of
  this: the loggers read no child of `.agent/` at all, so it is just another
  marker-absent shape without root `tasks/` and they skip. The underlying
  shell/Python divergence still exists between `gate-echo-lib.sh`, which globs
  and so skips dotfiles, and the Python copies, which use `iterdir()` and report
  `['.hidden']`. It predates this release, no supported flow can create such a
  lane (`validate_username` rejects a leading dot), and reconciling the two
  families is Phase 4 work recorded on `PB-LANE-RESOLUTION`.

- **Legitimate root-lane logging is preserved for projects that have a root
  `.agent/tasks/`, and given up for those that do not.** A blanket "never log to
  root" would satisfy every marker-absent assertion and silently kill logging
  for every legacy and mixed-layout project, where root `.agent/tasks/` makes
  the root itself a real lane (wrapper fixture scenario S6); that exemption
  stays, bound as an adverse control on
  `LegitimateRootLane.test_mixed_layout_without_a_marker_logs_to_the_root` and
  on the fixture's S16 legacy assertion.

  What is deliberately given up is the wider exemption: a project with no
  per-user lane and no root `tasks/` yet — reachably, a clone of a single-user
  project that committed `.agent/config.json`, since git tracks no empty
  `tasks/` — used to log the root and now logs nothing. `resolve_agent_dir`
  still answers the root there, so `tasks retro` and `tasks context` read a file
  nothing writes. That is a missing forensic log rather than cross-user
  contamination, it is owner-ratified, and it heals the moment anything creates
  `.agent/tasks/`, which `/playbook:init` does. The four affected shapes are
  pinned as such: `test_bare_agent_directory_writes_nothing`,
  `test_committed_config_only_writes_nothing`,
  `test_sessions_dir_only_writes_nothing` and
  `test_child_directory_without_tasks_writes_nothing`.

### Tests

- Suite **1341 → 1375**. Hermetic logger cases in
  `tests/test_provider_multiuser.py` execute the real bundled `bash-log.sh`
  through `bash` with `BASH_ENV` stripped — so a dogfooding host's installed
  logger cannot log a second time and mask the result — and assert filesystem
  effects for every policy answer, plus errexit survival on both the skip and
  the root path, deep-subdirectory walking, append-not-truncate, and that a
  non-lane sibling cannot cancel a real lane.
- **The strictness claim above is a test, not a sentence.**
  `TestResolverParity.test_logger_is_never_looser_than_lanes_without_marker`
  runs one nine-shape table through `provider/paths.py`, `tasks/core.py`,
  `gate-echo-lib.sh` and the bundled logger. It asserts the logger's own answer
  per shape and, separately, the direction of the difference: the logger may
  never write the root on a shape where a reference implementation reports that
  a per-user lane exists. That is the Critical fresh-clone case, and it is the
  half that must not regress when Phase 4 moves lane resolution behind one core.
- **The per-user-lane enumeration is deleted rather than made glob-safe.** It
  was the logger's only glob, in a file sourced into a shell it does not
  control, so its result followed the host shell's globbing state. Measured
  against `.agent/alice/tasks/` with no marker — a shape that must skip:
  default options and `shopt -s failglob` skipped, while `set -f`,
  `set -o noglob` and `GLOBIGNORE='*'` each silently wrote the **shared root**.
  Guarding one option at a time was the wrong shape of fix; the decision now
  reads two `-f`/`-d` tests and consults no directory listing at all.
- **Host shell options are now a tested dimension**, where previously none of it
  was covered: `HostShellOptionMatrix` runs all eleven in-policy shapes under
  fifteen option combinations — default, `set -e`, `set -u`, `set -o pipefail`,
  `failglob`, `nullglob`, `dotglob`, `extglob`, `failglob`/`nullglob`/`dotglob`
  each combined with `set -e`, a non-empty `GLOBIGNORE`, and the three that the
  deleted glob could not survive (`set -f`, `set -o noglob`, `GLOBIGNORE='*'`)
  — and requires exit 0, `ALIVE` on stdout, empty stderr, and the same lane
  decision in every one of the 165 cells. Those are the shapes and options that
  were measured, not a claim about arbitrary host shell state. A companion arm
  asserts the logger leaves the host's `failglob`, `nullglob` and `dotglob`
  exactly as it found them.
- The three new option cells were watched red against a real mutant rather than
  a synthetic one: the glob-based enumeration this release deletes, run on
  `.agent/alice/tasks/` with no marker, writes the shared root under `set -f`,
  `set -o noglob` and `GLOBIGNORE='*'`. The earlier-recorded mutants — the
  pre-fix blind root default and the blanket "never log to root" — were not
  rebuilt for this pass; they remain covered by the hermetic cases and by the S6
  adverse control. Every one of them is invisible to
  `tests/wrapper-multiuser-fixture.sh`, which is 274 / 0 either way, and that is
  why the hermetic cases and the shape table carry this guarantee.
- `tests/wrapper-multiuser-fixture.sh` under the corrected bundled logger is
  274 passed / 0 failed, where it was 272 / 2 before.

### Known limitations

- **`bash-log.zsh` received the structurally identical correction but was not
  executed.** zsh is not installed on the development host, so it was neither
  run nor syntax-checked. It is held to the Bash policy by source comparison
  (`ZshLoggerSourceParity`: lane-initialisation placement, that the bare root is
  assigned only under the `.agent/tasks/` guard, the marker validation arms, the
  one-line marker contract, and that its `unset` line still clears every
  variable it introduces) and its live execution is scheduled Phase 8 work — not
  claimed here. Those source assertions run against comment-stripped code:
  against the raw file text a comment naming an idiom satisfied the assertion
  for it, which had made one arm a false green. Deleting the enumeration also
  removed the `(N)` glob qualifier and the `break 2` that were this file's
  zsh-specific, unexecutable idioms, so the residual risk here is smaller than
  it was — but it is still source reasoning, not a measurement.
- **The installed copy at `~/.claude/bash-log.sh` is unchanged.** This release
  corrects the bundled artifact only; a machine keeps the old behavior until
  `/playbook:init` re-deploys the logger. The wrapper fixture run against the
  installed copy still fails the same two S15 assertions, by design.
- **The dot-named-lane shape no longer moves under `shopt -s dotglob` or a
  non-empty `GLOBIGNORE`**, measured: with the enumeration gone it skips under
  all three preludes, like every other marker-absent shape without root
  `tasks/`. `HostShellOptionMatrix.test_dotglob_no_longer_moves_the_dot_named
  _lane_answer` pins that. The shell/Python divergence itself is untouched and
  still Phase 4's to reconcile.
- macOS and Windows/Git Bash logger behavior, and live provider lane handoff,
  remain unverified.

## [1.5.33] — 2026-08-18

Independent verification hardening for 1.5.32. The original NUL-frame fix is
retained, and the release now also closes the other fail-opens and inspection
side effects reproduced by the verification pass.

### Fixed

- **A `\u0000` in the payload could shift the fused wire frame and make the gate
  fail open.** JSON can encode a literal NUL, and NUL is the `--emit-fields`
  record delimiter. So `file_path` = `"/tmp/a.py\u0000/.agent/x"` was emitted as
  *two* records: the path slot got `/tmp/a.py` and `/.agent/x` slid into the
  normpath slot — which `task-gate-hook` matches against its `*/.agent/*`
  exemption. Measured before the fix: `task-gate-hook` returned **0 (allow)** on
  an edit to real code with no active task. Every later record shifted too, so the
  payload record arrived empty and the hook's remaining parses read nothing.

  Each field is now truncated at its first NUL before it reaches the wire, and
  `normpath` is taken on the truncated value. Truncation rather than rejection
  because it is also the faithful reading: a path carrying an embedded NUL, had it
  reached any syscall, *is* `/tmp/a.py` — so the frame stays intact and the path is
  judged as the code file it actually names. Verified live: the same payload now
  returns 2, a genuine `.agent/` edit is still exempt, and a NUL in `tool_name`
  still routes to the right guard.

  Practical exposure was low — Claude Code does not emit `\u0000` in a path, and a
  NUL-bearing path cannot reach a syscall intact — but this was an enforcing gate
  failing open on input it could not represent, which the project's doctrine
  forbids outright regardless of likelihood.

- **The fused reader now validates the complete frame and fails closed.** A
  leading sentinel alone was not enough: truncated output, a dead normalizer,
  malformed JSON, a missing Python interpreter, or an invalid/blank tool name
  could still be mistaken for empty fields and allowed. The gate now accepts a
  fused record only after all seven NUL-terminated fields have been read. It
  retries extraction independently when possible and blocks when an enforcing
  field remains unreadable. Lone UTF-16 surrogates are made wire-safe, and the
  selected path is tool-specific (`notebook_path` for `NotebookEdit`,
  `file_path` otherwise), so a decoy field cannot redirect the exemption check.
  `Read` and other non-mutating tools remain usable without a path.

- **Risk-heading detection is strict about meaning, not spelling.** The close
  gate now recognizes BOM-prefixed, CRLF, case-varied, tab-separated and
  trailing-space forms of `## Risk`; ignores examples inside fenced code blocks;
  and treats duplicate headings as unclassified rather than choosing the most
  convenient value. Legacy task files with no real Risk heading keep the
  documented warning-only compatibility path.

- **Inspection flags now short-circuit every side-effecting path tested.** The
  tasks CLI handles nested `--help` before session GC or command dispatch;
  `playbook-codex`, `playbook-grok` and `playbook-agy` handle help before creating
  session state or launching a provider; and sandbox inspection flags remain dry
  even when combined with `--prompt`.

- **`sandbox --ro-project --prompt` now executes the containment it prints.**
  The prompt branch parsed `--ro-project` and repeatable `--rw` paths, and
  `--print-argv` displayed them correctly, but the live normal and streaming
  runners received a default `SubagentSpec(contain="repo")`. The project was
  therefore writable despite the read-only request, and every `--rw` exception
  was discarded. Both prompt runners now receive `contain="outdir"` plus all
  explicit writable paths. A real bubblewrap regression uses a fake agent to
  prove that a project-root write is denied while the named `--rw` path remains
  writable; composition tests cover normal and streaming execution.

- **Doctor collapse no longer hides an actionable live-install defect.** Only a
  semantically older, parseable foreign version is collapsed. Missing,
  malformed, equivalent and newer versions are enumerated, and a collapsed row
  carries every finding rather than only the first one.

- **The installed `BASH_ENV` logger no longer performs logging work inside hook
  processes.** Its DEBUG callback returns immediately for `*-hook` executables,
  avoiding repeated history scans and timestamps while preserving logging for
  actual provider shells.

### Notes

- Suite **1223 → 1247**. The added tests cover complete-frame truncation,
  unavailable-normalizer, surrogate, conflicting-path, malformed-input,
  risk-heading, provider-help, doctor-version, live `BASH_ENV`, and prompt-path
  read-only-containment regressions.

- Phase 0 remeasurement after the logger fix used three warmups followed by 10
  samples. Each sample sequentially ran `task-gate-hook`, `command-guard-hook`
  and `state-echo-hook` on the same minimal Bash payload. Mean total time was
  **142.62 ms** with `BASH_ENV` unset and **146.21 ms** with the bundled logger
  installed: **3.59 ms** measured overhead. Absolute wall-clock timing varied
  across samples and machine load; the supported conclusion is that the hook
  fast path removed the prior material logger penalty, not a universal latency.
- The parity oracle in `test_fused_payload_fields.py` now documents ONE deliberate
  divergence from pre-fusion behavior: for a NUL-bearing field the old
  `python3 -c` + bash command-substitution path was *itself* unsafe (bash drops the
  NUL, yielding `/tmp/a.py/.agent/x`, which also matched the exemption). Truncation
  is the only reading that blocks. The divergence is asserted explicitly rather
  than smuggled in as "parity".

## [1.5.32] — 2026-08-18

An audit-driven maintenance release: close the last close-gate fail-open, cut the
per-tool-call hook latency roughly in half, stop two "inspection" flags from
doing real work, and quiet the doctor's biggest source of noise.

### Fixed — soundness

- **An unset `## Risk` no longer walks through the close gate.** `panel_required_for`
  keys the review requirement to the risk class, and `unclassified` is in no
  class — so the whole risk-keyed bar evaluated to nothing and the task closed on
  a warning. That made "leave the field blank" the cheapest path through the
  strictest gate in the system, chosen by the same agent the gate exists to
  constrain. The close now separates two facts the old code conflated, using a
  discriminator already on disk (no new metadata):
  - **no `## Risk` heading at all** → a pre-1.5.0 task that was never offered the
    gate. Closes with the warning, exactly as before.
  - **heading present but unset**, or malformed (`## Risk: assertive` on one
    line) → the gate was offered and skipped. Blocks unless there is impl-review
    evidence or `--force --reason`, the same bar as `assertive`/`irreversible`.
  New `tasks.core.has_risk_section`; `close_decision` gains
  `risk_section_present` (default `False`, so a caller that cannot tell gets the
  lenient legacy path rather than an invented block). `## Risk Routing` in the
  light template is a gate checklist, not the field, and does not trigger it.

- **`sandbox --print-argv` was launching a live, billable agent.** The flag's own
  help says "print the fully wrapped argv instead of executing", but its check
  sat *after* the `--prompt` early-return in `provider/sandbox.py` — so
  `--print-argv --prompt "…"` ignored the dry run and spawned a real headless
  agent with the project writable. It now short-circuits before the run and
  prints the argv that path would actually use; `subagent.build_invocation()` was
  extracted so the dry run and the real run read from one builder and cannot
  drift. (`--list-agents`, `--list-models`, `--print-profile` were already
  correct.)

- **`init --help` provisioned the project and named it `--help`.** `$1` is a
  free-text display name and nothing rejected a flag in that position, so a full
  install ran — global touches (`~/.profile`, `~/.claude/settings.json`,
  `~/.claude/bash-log.sh`) included — and the mind map came out titled
  `# Mind Map — --help`. Now `-h/--help` prints usage and exits, any other
  leading-dash argument is refused with exit 2, and a real display name still
  lands in the mind map.

### Changed — performance

- **The hooks stop spending one interpreter per field.** `task-gate-hook` and
  `state-echo-hook` each ran `python3` four times per tool call — normalize, read
  `tool_name`, read `file_path`, `os.path.normpath` — parsing the same JSON three
  times, on hooks bound to every tool call. `hook-payload-normalize.py` gains
  `--emit-fields`, which does all of it in the one process already being spawned
  to normalize, and `command_guard.py` now normalizes in-process so its wrapper is
  a single `exec python3` instead of two piped interpreters.

  Measured on the same payload, project and machine (10 runs, `BASH_ENV` unset):

  | hook | before | after |
  |---|---|---|
  | `task-gate-hook` | 71 ms | 36 ms |
  | `command-guard-hook` | 47 ms | 28 ms |
  | `state-echo-hook` | 78 ms | 42 ms |
  | **per Bash tool call** | **196 ms** | **106 ms** |

  Records are NUL-delimited, because a command or path may contain newlines and a
  line-based protocol would hand the gate a different command than the one about
  to run. A leading sentinel (`pb-fields-v2`) and a complete seven-record read
  separate valid output from a missing interpreter, dead script or truncated
  frame. The enforcing gate retries independent extraction when possible and
  blocks when the payload remains unreadable. The task-014 byte-identity
  contract still holds: a native Claude payload comes back exactly as received.
  New `test_fused_payload_fields.py` pins the wire format and re-evaluates the old
  one-liners as a parity oracle across 16 payload shapes.

  Separately observed while measuring: the `BASH_ENV=~/.claude/bash-log.sh`
  wiring installed by `init` made its DEBUG trap fire inside every hook. 1.5.33
  short-circuits that callback for hook processes while retaining it in real
  provider shells.

### Changed — doctor

- **An older install copy is one line, not seven.** `tasks doctor` scans every
  hooks.json a host might load, which is right — a stale grok copy was the firing
  one in the AloVet bug. But on a real machine 6 of its 12 warnings came from a
  `~/.grok/marketplace-cache/…` copy of **v1.4.3** abandoned weeks earlier, and
  every one was correct *and* noise: a cache from before `command-guard-hook`
  existed necessarily fails the check for it. Warning fatigue is how the findings
  that matter get skimmed past. A foreign copy whose version differs from the
  running code now collapses to a single row naming the path, both versions, the
  finding count and the first finding. The collapse is version-keyed, not blanket:
  a foreign copy at the *same* version may be a live second install with a real
  defect, so those are still enumerated. 1.5.33 further restricts the collapse to
  semantically older, parseable versions and includes every finding in the
  summary row. `tasks doctor --verbose` enumerates everything.

### Notes

- Suite **1159 → 1217**. Every fix watched red first, each with a negative
  control. Two behavior-change tests in `test_light_template.py` were rewritten
  rather than deleted: they encoded the old warn-and-pass close, and now encode
  the block plus the preserved legacy path.
- Superseded by 1.5.33: an enforcing gate with an unavailable `python3` now
  blocks instead of treating the payload as empty.

## [1.5.31] — 2026-08-18

Extend the destructive-command interlock to **all three providers** (was Claude
only) — so a dangerous command can't run by accident under Codex or Grok either.

### Changed / Added

- **Codex**: `render_playbook_hooks` now registers `command_guard.py` as a
  `PreToolUse` hook scoped to `^exec_command$` (Codex *can* pre-block exec; it
  just didn't). `command_guard.classify_command` now understands Codex's
  `exec_command` shapes — a string, an argv **list**, and the `bash -lc "<script>"`
  wrapper (unwrapped so the real command isn't hidden behind the interpreter).
- **Grok**: the always-trusted global enforcement (`build_enforcement_hooks_payload`)
  gains a second `PreToolUse` matcher (`Bash|Shell|run_terminal_command`) →
  `command-guard-hook`. The hook wrapper now normalizes the payload first, so
  grok's camelCase / renamed shell tools (`Shell`, `run_terminal_command`) are
  seen the same as Claude's `Bash`.
- Fixed a crash in the guard's block message when the command was an argv list
  (Codex) — it now coerces to a display string before formatting.
- Provider mirror (`scripts/lib/provider/`) re-synced; docs (architecture,
  configuration) updated to say the interlock is active on Claude, grok, codex.
  New tests: `test_codex_command_guard.py` + grok-parity + grok-normalize cases.



Add the missing **deterministic** safety layer, so a dangerous command can't run
by accident — not just by judgment. Analysis: the sandbox contains filesystem
blast radius and the close contract catches under-leveling at close, but a
destructive/outward shell command (`rm -rf /`, `git push --force`, `curl|sh`, a
DB `DROP`) had no mechanical guard *before* it ran.

### Added

- **Destructive-command interlock** (`scripts/command_guard.py` +
  `command-guard-hook`, a second PreToolUse hook on shell tools). Blocks
  unambiguous high-blast/irreversible commands until acknowledged
  (`PLAYBOOK_ALLOW_DANGEROUS=1`, or inside an `irreversible`-classified task).
  Designed to not become a problem itself: **conservative** (matches only at a
  command position — `echo "rm -rf /"` / `grep "DROP TABLE"` do not trip it; a
  relative `rm -rf ./build` is fine; `--force-with-lease` is allowed), **fails
  OPEN** on any internal error (never wedges a session), **config-extensible**
  (`dangerous_commands: [regex]`) and disable-able (`command_guard: false`).
  Spec'd by a decision-fixture set (dangerous MUST block, look-alikes MUST
  allow) in `tests/test_command_guard.py`. Claude path today; codex/grok Bash
  guarding is a follow-up.
- `hooks_check.EXPECTED_HOOKS` generalized to event→list so the hook-integrity
  check (and `tasks doctor`) now guard the new hook's presence + quoting too.

### Note

This is a safety interlock against the agent's *mistake*, not an adversary, and
no pattern set is exhaustive — the real guarantee for filesystem blast radius is
still running the agent in the **sandbox** (OS-level). Docs (architecture,
configuration, CLAUDE.md ceremony backstop) updated to say so.



Harden the ceremony-classification protocol so it doesn't under-level the
highest-risk requests. Adversarial review of 1.5.28's protocol found it led with
"is it code?" — so its "no code → just do it" bucket silently waved through two
genuinely dangerous cases: **destructive/outward shell & git ops** (`rm`,
force-push, deploy, DB/network/payment side effects — all `irreversible`) and
**assertive docs** (a README/benchmark/"verified" claim — `assertive` even as a
one-word edit).

### Changed

- **Ceremony protocol is now RISK-first, size-second** (CLAUDE.md template +
  `/playbook` skill). A leading **high-risk trigger list** — deletes/migrates
  data; secrets/auth/permissions; a destructive or outward command; a claim about
  the world; anything `git revert` won't undo in the world — forces `light`/full
  no matter how small the diff looks, overriding the "no task" shortcut. The
  size-based buckets handle only what clears those triggers. Also states the
  honest backstop: the code-edit hook mechanically catches a mis-leveled *code*
  change, but shell/git/docs have no such gate, so the risk triggers are their
  only guard. (A prose protocol applied by judgment can't be infallible; this
  makes it risk-first, safe-defaulted, and explicit about the cases that bite.)



Match ceremony to risk — so quick work stays quick and the trust machinery aims
where it matters, and the agent chooses the level (the user shouldn't have to).

### Changed

- **`/playbook:init` now seeds a risk-gated close policy** —
  `panel_required_for: ["assertive", "irreversible"]` instead of `"all"`.
  Reversible work (the common case, incl. `quick`/`light`) closes on
  verify+single-judge evidence with no panel wait; only changes to
  claims-about-the-world (`assertive`) and data/publish (`irreversible`) require a
  quorum-PASS panel. `"all"` remains available for max strictness. Fixes the
  papercut where a trivial reversible `quick` task couldn't close without
  `--force`. `scripts/init` seed + `_doc`; docs (cli, init.md, usage) reconciled;
  new test pins the seed against drift.

### Added

- **Ceremony auto-classification guidance** (CLAUDE.md template + `/playbook`
  skill): the agent now decides the level itself — no code → no task; trivial +
  clearly reversible → `quick`; small-but-real / touches a claim/config/data /
  reversibility unclear → `light`; multi-step / new subsystem / uncertain /
  irreversible / assertive → a full type; `freehand` only when the user asks for
  no-gate pairing. On the line between two levels it takes the heavier; it asks
  the user **only** when it genuinely can't gauge risk or scope, and even then
  leads with a recommendation biased to the safer option. Better safe than sorry.



### Added

- **`testing` skill** — a method for the doctrine the plugin already preaches
  ("expand test coverage every task"; the template puts a test gate after every
  work gate). The skill derives test needs from three frozen ledgers —
  specification, human-signal (mined from `.agent/chat_log.md` via `tasks context`
  / `tasks log`), and architecture-risk — challenges them against a portable
  `culture.md` of cross-project testing lessons, classifies the existing suite
  (STRONG / PARTIAL / WRONG_BOUNDARY / MISSING / …), and reports ≤5 ranked
  confidence upgrades. Its core habits (preserve the betrayal not just the fix;
  assert forbidden effects; prove at the owning boundary; calibrate a
  consequential test by making it fail for the intended reason first) are exactly
  the discipline the playbook's own suite follows. This is the sixth
  harness-discoverable skill (docs/cli.md + architecture.md updated). Adapted from
  `horiacristescu/playbook-harness` (same lineage/author).



Verification pass over the 1.5.21–1.5.25 context-economy arc: an independent
3-way adversarial audit (recall/bootstrap, the map audit-checks, compact/
environment), every finding re-reproduced by hand before fixing. No high/medium
defects, no crashes on the enforcement path, advisory-never-fails intact. Fixed
the surfaced edges red-first.

### Fixed

- **`tasks recall <unicode-digit>` crashed** — `str.isdigit()` is true for `²`/`⁵`/`₃`
  but `int()` rejects them, so node-id mode threw an uncaught `ValueError`. Now
  ASCII-guarded; a superscript falls through to keyword search.
- **`compact` silently folded a CRLF `task.md` to LF** (whole file) and archived
  the block non-verbatim — it now reads/writes with newline preservation, so a
  Windows task.md keeps its line endings and the archive is byte-exact.
- **`compact` wrote `task.md` non-atomically** — a failed write could leave the
  block in BOTH files (a retry double-appended) and surfaced a raw traceback.
  Now: append archive → **atomic** task.md write (temp + `os.replace`) → on
  failure, roll the archive back and exit with a clean message. All-or-nothing.
- **`compact` treated markers inside a ``` fence as real** and skipped empty
  blocks poorly — `_blocks` is now fence-aware, and an empty block is left in
  place instead of writing a hollow archive entry.
- **`mindmap-wellformed` missed a self-referencing island** — a node citing its
  own id counted as reaching itself. Self-links no longer count toward
  reachability.
- **`mindmap-node-freshness` could flag a git-rm'd path** (its `git log` history
  survives), overlapping the staleness check — it now skips paths absent from
  disk, matching its contract.
- **`recall` gave no signal on an unbalanced code fence** (unlike the rest of the
  module) and **silently showed the last of a duplicate id** — both now warn.
  A dangling link in the map preamble reads as "preamble", not "node [None]".
  The bootstrap index notice is grammatical for a single indexed node.



Context-economy pass, part 5: the map's *structure* is now checked mechanically,
not just its content — so "how the map is written" stops depending on the author
remembering the checklist. Pure-benefit (advisory, fires only on real defects).

### Added

- **`mindmap-wellformed` audit check** — three structural defects, all
  unambiguous against the documented format: a **duplicate node id** (retrieval
  can only keep one), a node with **no `**bold title**`** (the index shows a
  degenerate label), and an **unreachable node** — a non-routing node nothing
  links to, i.e. dead memory the index surfaces but navigation never reaches.
  Fence-aware; routing nodes (first five) are exempt from the reachability check;
  a node's own definition token doesn't count as a link to itself. Advisory;
  `audit.wellformed_severity` raises it. New `check_mindmap_wellformed` in
  `tasks/audit.py`. This is the fourth built-in mind-map check (with stale-refs,
  node-freshness, dangling-links).

### Changed

- **`/mindmap` writing guidance tightened** — a system node should cite the
  files it owns (those citations are its freshness *anchor*), add a keyword alias
  when its search terms differ from its title, and earn its place with at least
  one incoming link. The guidance now names the mechanical checks that enforce it.



Context-economy pass, part 4: sharper retrieval — "better than grep" within the
plugin's stdlib-only, offline, portable contract (no embeddings/vector index by
design; those would trade away portability).

### Changed

- **`tasks recall <keyword…>` is now a RANKED relevance search** (BM25 + plural
  stemming), best node first, across both map tiers — replacing the old
  substring-AND that returned nothing for cross-node queries like `policy
  storage`. A node matching more terms (and rarer ones) ranks higher without
  excluding partial matches. New `_tokenize` / `_build_corpus` / `_rank_nodes`
  in `tasks/mindmap.py`; node-id mode unchanged.
- **Mind-map nodes can declare keyword aliases** — `<!-- keywords: login,
  credentials -->` on any line of a node — so `recall` finds it by meaning, not
  just its wording (weighted 3× in ranking). Documented in the `/mindmap` skill.

### Added

- **`tasks environment` suggests faster-than-grep search tools** — a new
  "Search / navigation" category recommends `rg` (ripgrep), `ast-grep`/`sg`
  (structural code search), and `fd`, with install hints. Advisory; the harness
  Grep tool is already ripgrep-backed, so a miss is a nicety not a failure.
- **"Finding things fast" guidance** in the `/playbook` skill: reach for the
  language server (go-to-def / find-refs) and `ast-grep` for CODE, `tasks recall`
  for the mind map, and `grep`/`rg` for plain text — the sharpest tool per job,
  not always grep.



Context-economy pass, part 3: internal-consistency check for the map.

### Added

- **`mindmap-dangling-links` audit check** — a `[N]` cross-link that points at a
  node id defined nowhere in `MIND_MAP.md` is a dead end the agent follows (or
  tries to `recall`). This is the internal-consistency complement to the
  staleness checks (those compare the map to the code; this compares the map to
  itself). Fence-aware and precise by construction — only `[<digits>]` tokens
  count, so markdown checkboxes (`- [ ]`), `[text](url)` links, version tags
  (`[1.5.0]`), and range tokens (`[1-5]`) never register — and each finding names
  the SOURCE node so the drift is fixable. Advisory; `audit.dangling_links_severity`
  raises it. New `check_mindmap_dangling_links` in `tasks/audit.py`.



Context-economy pass, part 2: complete the retrieval loop. 1.5.21 gave bootstrap
a mind-map INDEX (routing nodes + titled TOC) but only for `MIND_MAP.md` — a
summarized `↗` node's deep detail lives in `MIND_MAP_OVERFLOW.md`, which had no
index and no retrieval path, so "find exactly the node you need" broke at the
overflow boundary.

### Added

- **`tasks recall <id | keyword…>`** — the fetch half of the index, across both
  tiers. `tasks recall 12` prints node [12] from `MIND_MAP.md` **and** from
  `MIND_MAP_OVERFLOW.md` (labeled), so a node's full content is one command
  instead of "know the overflow file exists and grep it by hand." `tasks recall
  auth policy` lists `[N] Title` for every node in either file containing all the
  words (AND, case-insensitive) — a topic resolves to node ids you then recall in
  full. New `_iter_map_nodes` / `cmd_recall` in `tasks/mindmap.py`, wired into
  `cli.py` (COMMANDS + dispatch + baseline).

### Changed

- **Bootstrap index now points into the overflow tier.** When
  `MIND_MAP_OVERFLOW.md` exists, the index notice says so and directs fetches to
  `tasks recall <N>` (spans both files) instead of a main-only grep.
- The task-template References gate, `CLAUDE.md` template, and CLI help/usage now
  drive first-contact context-gathering through `tasks recall` rather than a raw
  `grep MIND_MAP.md` that misses overflow.



Context-economy pass: make the agent load the information it needs, retain what
matters out of the way, and stop carrying what's of no use. Four levers around
*find / use / retain / ignore*.

### Added

- **Bootstrap loads a mind-map INDEX, not a full dump** (*find*). Once
  `MIND_MAP.md` grows past ~8 KB, `tasks bootstrap` prints routing nodes [1]-[5]
  in full plus a one-line **titled** TOC of every other node and the grep to
  fetch one — orientation now costs the map's *shape*, not thousands of tokens of
  subsystem prose the task never reads (69% smaller on a real 18-node fixture; far
  more on large maps). A small map still prints whole. The judge/review path
  (`_load_mind_map`) is untouched — auditing needs whole nodes. New
  `_bootstrap_mind_map` / `_mind_map_toc` in `tasks/mindmap.py`.
- **`tasks compact <N>`** (*retain*) — the mechanical half of the sanctioned
  task.md compaction. Wrap cold review-round narrative in
  `<!-- archive:start -->` … `<!-- archive:end -->`; the command appends each
  block VERBATIM to `task-archive.md` and leaves a pointer. It refuses to move a
  gate, a `<!-- pin -->`, a protected section heading, or a block behind an
  unmatched/nested marker — a mismark fails loud, never amputates the trace.
  `--dry-run` previews. New `tasks/compact.py`.
- **`mindmap-node-freshness` audit check** (*ignore*) — complements the existing
  deleted-path check: a node whose cited code changed in ≥2 commits *since the
  node was last edited* (via `git blame`) is flagged as stale institutional
  memory. Nodes already cite real file paths, so their own citations are the
  anchor — no new syntax. Git-only, advisory, high-precision on purpose; tunable
  via `audit.node_freshness_commits` / `node_freshness_severity`, off with
  `audit.node_freshness: false`.

### Changed

- **Mind map is a map, not a log** (*ignore*). The `/mindmap` skill and
  generation guidance now say the map answers "how does this work / where is it,"
  never "what happened when": no commit hashes, dates, changelog lines, or a
  "Development History" node. When an evolution carries a design lesson, fold the
  *reason* (not the hash) into the owning subsystem node. Git holds the *when*.
- `mind_map_header()` is now honest about index-vs-full output; the bootstrap docs,
  CLAUDE.md template CLI list, and task sticker document `tasks compact`.



Hardening from an independent 3-way verification pass over 1.5.16–1.5.19 (every
finding re-reproduced by hand). The audit confirmed the enforcement core (F2/F3),
the dead-mirror deletion, and the doctor hermeticity are all correct/safe — no
High/Med enforcement holes. These are the Med/Low edges it surfaced.

### Fixed

- **`tasks detect-verify` crashed on a valid-but-non-object `package.json`**
  (a bare list/string/number/bool → `AttributeError`), violating its "never
  raises" contract on the init path. Guarded with an `isinstance(dict)` check.
- **`tasks detect-verify` mistook a Makefile `:=` variable for a target.**
  `test := build/out` yielded a suggested `make test` that fails at verify time
  ("No rule to make target"). Immediate-assignment (`:=`/`::=`) variables are no
  longer read as targets.
- **`tasks environment` emitted a redirection fd digit as a bogus tool.**
  A verify command like `2>&1` produced a spurious `verify tool: 2` warning
  ("close will fail"). Pure-digit tokens are dropped (real digit-led tools like
  `7z`/`2to3` still pass). Also: newlines in a verify string are now real command
  separators, so a multi-line verify no longer silently misses every line but
  the first.
- **Gate classifier parity held only on realistic paths.** bash and Python
  disagreed on dots-then-name basenames (`..py`, `...toml`) — bash saw an
  extension, Python's `os.path.splitext` ignores leading dots. bash now strips
  leading dots to match, and Python `rstrip`s a trailing newline to match bash's
  ext extraction. Added parity vectors so the guard covers this class.
- **F3 done-detection was stricter than the CLI authority.** The gate checked
  `status == "done"` while `tasks`'s own `_is_done` uses `startswith("done")`,
  so a `done (2026-…)` status let a stale pointer authorize an edit. Both gate
  surfaces (bash + codex) now use `startswith("done")` and tolerate an indented
  `## Status` header, matching `core._extract_status`/`_is_done`.

### Changed

- **Gate now also treats `.mjs .cjs .scss .less .proto .graphql .gradle` as code**
  (consistent with the strict 1.5.18 decision — module/preprocessor/schema/build
  files that were previously ungated outside a code dir).

### Docs

- Corrected the stale "no-network" description of `tasks models detect` in
  `cli.md` / `configuration.md` (grok's listing is login-aware). Documented
  `tasks audit` / `blocked` / `parked` in the CLI reference. Fixed comments that
  attributed F2 to 1.5.17 (it shipped in 1.5.18). Refreshed the README drift
  baseline (`docs/readme-audit-baseline.json`) from 1.5.13 to this release.

## [1.5.19] — 2026-08-17

Closes the last backlog item: the interactive init's verify-command detection is
now a deterministic, tested helper instead of the agent free-form-guessing.

### Added

- **`tasks detect-verify [--json]`.** Inspects a project's toolchains and prints
  a single full-verify command (typecheck **and** tests **and** lint) chained
  with ` && ` — Python (`pytest`/`mypy`/`pyright`/`ruff`/`flake8`), Node
  (`package.json` scripts), Rust (`cargo test` + `clippy`), Go (`go test` +
  `vet`), and a `Makefile` `test`/`check`/`lint` target as a fallback. Reads
  small config files only, never executes anything, and prints a note (not a
  guessed command) when it detects nothing. New `tasks/verify_detect.py`.

### Changed

- **`/playbook:init` step 3** now runs `tasks detect-verify` and shows its
  suggestion for the user to confirm/correct, instead of instructing the agent
  to inspect the repo by hand — deterministic, and the same detection is now
  reusable/tested. The confirm step still exists to catch a missed check.

## [1.5.18] — 2026-08-17

F2: the code-file gate classifier now means the same thing under every provider.
The bash gate (default Claude path) and the Python gate (opt-in codex apply_patch
path) had diverged in **both** directions; they are now reconciled onto one
definition, pinned by a shared parity test.

### Fixed

- **Codex under-gated real source languages.** The Python `_is_code_file_path`
  omitted ~20 extensions the bash gate has always covered — `.php .vue .svelte
  .swift .kt .kts .dart .cs .scala .zig .lua .ex .exs .ml .mli .tf .hpp .r .m
  .mm` — so under the codex apply_patch gate, editing e.g. a `.php` or `.vue`
  file with no active task was silently ALLOWED. All are gated now.
- Directory + doc/data handling reconciled: a doc/data file (`.md .txt .json
  .png .svg .jpg .jpeg .gif .ico .webp .pdf .lock .csv`) is never code, even
  inside a code dir; an undecided/extensionless path is code iff a component is
  a known code dir (`scripts bin src hooks lib cmd`). Extension matching is
  case-insensitive on both surfaces.

### Changed — behavior

- **`.css .html .sql .yaml .yml .toml` now require an active task on the default
  Claude path too** (strict reconciliation — they were already gated under
  codex). Editing a stylesheet, a SQL/migration file, or a YAML/TOML config
  without an active task will now be blocked by the gate. `.json` stays exempt
  as data (deliberate — it is not in the strict set). If a quick config tweak is
  blocked, start a `quick`/`light` task or edit via a raw shell redirection (the
  documented honest-agent boundary).

### Internal

- `is_code_file_path` moved from `scripts/task-gate-hook` into the sourced
  `scripts/gate-echo-lib.sh` so the parity test can exercise it in isolation.
- New `tests/test_gate_classifier_parity.py` asserts the bash and Python
  classifiers agree with an expected verdict over a 60-case vector table (the
  same "one property over a fixture table" guard used for gate-parser and
  resolver parity). Edit the two lists together or it fails loudly.

## [1.5.17] — 2026-08-17

Backlog cleanup: retire the dead source mirror, make one doctor check hermetic,
and close the small accepted-design nits (F3/F10/F8/N3). No feature changes.

### Removed

- **The dead `scripts/lib/tasks/` mirror (7,329 lines, pinned at 1.4.1).** It
  was never on any live path — the Codex hooks bootstrap `scripts/lib/` but only
  import `provider.*` (all stdlib at hook time); the adapters that `from tasks.*`
  only run under the canonical CLI. Deleting it ends the "which tree actually
  runs?" ambiguity. `provider/paths.py` docstring updated; the version-parity
  guard now asserts the mirror STAYS gone (a wholesale re-copy would silently
  reintroduce a shadow tree). The "both copies" prompt/template drift tests drop
  their mirror arm.

### Fixed

- **F3 — the code-edit gate authorized a pointer resolving to a DONE task.**
  Defense-in-depth: a stale/hand-written pointer to a closed task no longer
  keeps the gate open. Added on BOTH surfaces (bash `task-gate-hook` + Codex
  `has_active_task`) so it doesn't create a new parity gap. `tasks work <N>`
  reopens a done task (status → in_progress) before editing, so a real resume is
  unaffected; an unparsable status is never treated as "done" (no new false
  block). Mirror re-synced.
- **F10 — `_safe_int` could return a negative** for an absurdly long all-digit
  input (>18 digits overflow bash's signed 64-bit `$(( ))`). Clamped to 0, so
  the "non-negative" contract holds. (Negatives were already rejected.)
- **N3 — a merge renumber silently normalized non-UTF-8 bytes in `chat_log.md`
  to U+FFFD.** Behavior stays crash-safe on purpose (surrogateescape would only
  relocate the failure to the next strict-encode site — worse for a merge), but
  the rewrite now WARNS when it is about to normalize bytes, instead of losing
  them silently. New `_read_text_lossy` reports lossiness.
- **`tasks doctor`'s Python≡bash resolver-parity check false-FAILed when the
  suite ran detached** (background CI). With no agent process on the ancestry,
  both resolvers legitimately fall back to a process-LOCAL `pid-<ppid>` that
  differs; the check now requires exact equality only when a real agent root
  exists (both converge on it) and structural (`pid-…`) agreement otherwise.
  The production guarantee (env-authoritative via PLAYBOOK_SESSION_ID) is
  unchanged.

### Added

- **`{{INTENT}}` token for custom playbooks (F8).** The `[intent]` argument to
  `tasks new` now reaches a custom `.agent/playbooks/<type>.md` template via an
  explicit `{{INTENT}}` token (alongside `{{NNN}}`/`{{TITLE}}`); a playbook that
  doesn't use it is unchanged. Documented in the playbooks README.

## [1.5.16] — 2026-08-17

Verification-pass fixes for 1.5.14/1.5.15. An independent three-way adversarial
audit of those releases (each finding re-reproduced by hand in scratch dirs)
turned up seven real defects the 1003-green suite missed — all fixed here
red-first. The `run_select` refactor and the end-to-end wiring were audited
CLEAN; these are the edges around the new `models detect/set` and `environment`
surfaces.

### Fixed

- **`tasks environment` invented tool names from ordinary verify commands.** The
  verify-tool detector split on shell operators with no awareness of quotes,
  subshells, `bash -c`, or keywords, so `(cd sub && pytest)` warned about `(cd`
  and `pytest)`, `grep "a|b" .` warned about `b"`, and an installed tool behind
  a subshell close-paren (`grep)`) was reported *missing* — a false "close will
  fail" advisory. `_command_words` is now `shlex`-based (quote/operator-aware),
  steps past `VAR=val`/`sudo`/`env` prefixes and shell keywords, ignores
  redirection targets, and emits nothing on a parse ambiguity rather than
  invent a token.
- **`tasks environment` could crash where `Path.home()` is undeterminable.**
  `Path.home()` sat outside the guard in `_logging_item`, so on a container run
  as an arbitrary UID with no `HOME` the "never raises" `environment_report`
  raised and the bare `tasks environment` command exited non-zero with a
  traceback. Moved inside the try. (`tasks doctor` was already safe — it wraps
  the call.)
- **`tasks models set --default-judge X` (no `--panel`) silently froze the
  shipped panel** into `.agent/models.json`, so the project stopped tracking
  future plugin-panel upgrades. `_write_panel` now leaves the `panel` key
  untouched when no panel is passed; set validates/audits only the specs it's
  actually changing.
- **`tasks models set`/`select` crashed on a models.json that is valid JSON but
  not an object** (e.g. a bare list) — `AttributeError` instead of the
  documented "start fresh". Guarded in `_read_existing_models` and in
  `provider/sandbox.py`'s `_parse_models_json` / `_parse_judge_config` (whose
  own docstrings promise `{}` on any shape error). Mirror re-synced.
- **`tasks models set --panel ""` cleared every judge with no warning.** It now
  prints a loud warning that the panel is empty (panel-review will have no
  seats).
- **Misleading flag parsing.** `set --panel --force` silently consumed `--force`
  as the panel value; a value-flag at end-of-args reported "unknown flag". Both
  now report "missing value for <flag>".
- **`tasks models detect` / `/playbook:init` over-claimed "no network."** `grok
  models` is login-aware (a server call), so the docs now say "no live model
  probe" and note the listing is time-bounded, not offline. `--json
  --suggest-only` now filters the JSON too, and init.md's live-check step no
  longer contradicts its own execution order.

## [1.5.15] — 2026-08-17

`/playbook:init` and `tasks doctor` now tell you which optional tools would make
playbook run *optimally*, and how to get the ones you're missing. Suggest-only —
nothing is ever auto-installed, and none of it fails a gate.

### Added

- **`tasks environment [--json] [--suggest-only]`.** Advisory inventory across
  four categories, each with an install hint: (1) **extra vendor agent CLIs**
  (`codex`/`agy`/`grok`/`pi`) not installed — the headline one, because a panel
  that spans vendors is the whole reason a panel exists (never trust one model;
  let them disagree); (2) **sandbox containment** (`.claude/bin/sandbox` needs
  Linux `bubblewrap` / macOS seatbelt); (3) **verify-command tooling** — the
  leading binary of each segment of the project's declared `verify` command that
  isn't on PATH (a missing one fails close, so it's flagged as a warning);
  (4) the **shell-command-logging** (`BASH_ENV`) wiring. Best-effort install
  hints live in an editable table (`tasks/environment.py`) since package names
  drift; where no reliable command is known, the hint points at the vendor's
  docs rather than fabricating one.

### Changed

- **`tasks doctor`** gained an advisory environment section (never affects the
  exit code — informational, like its other advisories).
- **`/playbook:init`** now surfaces the suggestions as a step (after the panel
  and verify steps), relaying them with install hints — and offers to re-run the
  panel step if the user installs a new vendor CLI. It never installs anything.

## [1.5.14] — 2026-08-17

Interactive `/playbook:init` — the panel becomes a per-machine choice instead of
a fixed shipped default. Everything else stays the single correct value on
purpose: the panel still gates **every** close (`panel_required_for: "all"`),
merges run only via `/playbook:merge`, and standing gates keep their optimal
defaults. The only thing that genuinely varies from machine to machine is *which
agent CLIs are installed*, so that — and the project's verify command — is all
init now asks about.

### Added

- **`tasks models detect [--json]`.** Fast, no-network inventory of the installed
  agent CLIs (claude / codex / agy / grok / pi) with each one's selectable models
  and — for codex and grok — the reasoning-effort levels each model accepts. Reads
  only local surfaces (`~/.codex/models_cache.json`, Claude Code's `settings.json`)
  and the two cheap listing commands (`agy models`, `grok models`); no model is
  live-probed. This is the menu `/playbook:init` offers before it writes a panel.
- **`tasks models set --panel a,b --default-judge c [--force]`.** Non-interactive
  twin of `tasks models select`: writes `.agent/models.json` with the *same* spec
  validation and no-probe availability audit, but driven by flags instead of
  prompts. A dead pin aborts (exit 1) rather than prompting "write anyway?" —
  `--force` overrides. `/playbook:init` uses it to persist the user's panel choice.

### Changed

- **`/playbook:init` is now interactive.** After the mechanical setup it (1) runs
  `models detect`, asks the user which models + effort go on the review panel
  (recommending ≥2, cross-vendor when available; all-Claude `opus, sonnet` is the
  default when only Claude is installed), optionally live-verifies the chosen panel
  with `models check`, and writes it with `models set`; and (2) auto-detects the
  project's full verify command (typecheck **and** tests **and** lint), confirms it
  with the user — the confirm step catches a *missed* check, never narrows the bar —
  and writes it to `.agent/config.json`. A headless run keeps the shipped
  all-Claude defaults and leaves `verify` unset rather than guessing.
- `models_check.py` internals refactored so `select` and `set` share one spec
  validator (`spec_error`), one no-probe audit (`_audit_proposed`), and one atomic
  writer (`_write_panel`) — no behavior change to `select`, whose tests are
  unchanged.

## [1.5.13] — 2026-08-17

The final-acceptance batch: a five-model review panel (opus, sonnet, two codex
variants, grok — two rounds, invoked through the plugin's own adapters) audited
1.5.12 against the 952-green suite. Every candidate finding was re-reproduced by
hand in scratch dirs (judges advise; reproductions decide); the CONFIRMED,
in-scope defects are fixed here red-first, each with a test that fails on 1.5.12.
The named foundation (C1–C5, N1, N2, NEW-1, NEW-2, B1, B2) was independently
re-reproduced DEAD and did not regress. See `verification-report-1.5.12-panel.md`.

### Fixed

- **`tasks work <slug>`/`work <NNN-slug>` stranded the agent (F11).** `tasks list`
  shows the folder name (`001-fix-widget`), so an agent naturally passed it back to
  `work`; activation printed the briefing and exited 0 but wrote the raw slug as the
  session pointer, which the numeric-only code-edit gate (N2) then rejected —
  blocking the very next edit as "No active task". `cmd_work` now canonicalizes the
  pointer to the resolved folder's number, so activation and the gate agree.
- **Custom-playbook stubs lost their gates on activation (F7 + F18).** A `--stub`
  of a custom `.agent/playbooks/<type>.md` type expanded to the base Build template
  instead of the custom playbook — every custom gate silently vanished — and a
  hyphenated type name (`sp-eval`, the flagship example) never expanded at all (the
  stub marker regex was `\w+`). Stub-expansion now mirrors `create_task`'s dispatch
  (`_find_custom_playbook`, whole-file) and the marker regex accepts `-`.
- **`tasks new --stub light <name> <intent>` dropped the intent on activation
  (F6).** B1 fixed the direct `tasks new light` path but not its stub-expansion twin,
  whose placeholder list omitted the `light` template's Intent placeholder. Added.

### Security / robustness

- **A `panel_required_for` typo silently disabled the seeded close gate (F5).**
  `resolve_panel_required` matched only exact lowercase `"all"`, so `"ALL"`/`"All"`
  fell through to "no panel required" — a case typo quietly downgrading the seeded
  safety posture. Now case-folds the keyword and WARNS (never silently) on any other
  unrecognized scalar. (Adjudicated non-critical — single-judge/risk-keyed evidence
  still applied — but a real fail-open on a near-miss of the seeded default.)

### Changed

- **`tasks intent`'s unset-config fallback is now all-Claude (F13).** It fell back
  to `codex`/`CodexAdapter` when `default_judge` was unset, while the review path
  falls back to `claude` — re-exposing the codex adapter on that edge path. Aligned
  to `opus`/`ClaudeAdapter`.

### Docs

- **`review.py` no longer claims the default judge "ships codex" (F12).** The usage
  string and a code comment were stale after 1.5.12 flipped `default_judge` to
  `opus`; four of five panel models flagged it. Reconciled to the all-Claude default.
- **`commands/upgrade.md` now points at `/playbook:init`, not bare `/init` (F14).**
  Claude Code's built-in `/init` is a generic CLAUDE.md generator that runs none of
  the mechanical upgrade work (wrappers, hooks, `.gitignore`, CLAUDE.md merge) that
  `scripts/init` does; the documented upgrade flow silently skipped all of it.
- **`docs/cli.md` + `docs/architecture.md`: "six skill bundles" → five (F15)**,
  since only five carry a `SKILL.md`; `skills/tasks/` is the task-template asset, not
  a harness-discoverable skill.
- **`docs/cli.md` + the `work done --help` text: reconciled the `tasks init` vs
  `/playbook:init` split (F16/F19).** The bare CLI `tasks init` creates the `.agent/`
  structure + `CLAUDE.md` + `MIND_MAP.md`; the full scaffolding (`.claude/bin/`
  wrappers, `settings.json`, `.agent/config.json` with the seeded
  `panel_required_for: "all"`, `.gitignore`) is done by `scripts/init` via
  `/playbook:init`. The docs had attributed the wrappers/settings/seed to the CLI.
- **`scripts/task-gate-hook` BLOCKED text now lists the real task types (F17)** —
  it advertised `explore/review/decision/test` (all rejected) and omitted seven real
  ones. Fixed both messages and the matching `README.md` overview line.
- **`provider/policy._is_code_file_path` docstring corrected (F2):** it claimed to
  mirror the bash `is_code_file_path` but adds `.css/.html/.sql/.yaml/.yml/.toml`.
  Documented the real (opt-in codex-only) divergence; enforcement alignment is
  deferred to a codex-parity pass (the default all-Claude path is self-consistent).

### Notes — panel findings adjudicated as accepted/deferred (not fixed)

- **Arbitrary Bash writes are not gated (F1/F9/F10-write):** the PreToolUse gate
  enforces the code-EDIT tools; parsing arbitrary shell (or the `.agent`-writable
  stop-hook counters) is out of scope by the "no new gates" decree and the
  honest-agent threat model. 0/5 panel CONFIRM as a defect.
- **Judge tamper backstop / already-dirty files (F4):** documented "known gap" and
  unreachable under the read-only judge sandbox (`project_writable=False`).
- **Done-task pointer self-authorizes (F3):** the I2 contract is "pointer resolves
  to a real task"; `work done` clears the pointer, so this needs a manual `.agent`
  re-point at a done task — the accepted self-auth family, narrow.
- **`_safe_int` wraps a 2^64-1 input to a negative (F10):** a docstring contract nit
  with no exploit beyond the already-accepted agent-writable-counter case.
- **Custom-playbook `[intent]` substitution (F8):** custom playbooks document only
  `{{NNN}}`/`{{TITLE}}`; intent prefill is not a promised contract for them.

## [1.5.12] — 2026-08-16

The publish-readiness batch: a four-way independent audit of 1.5.11 (core
workflow, enforcement/security, review/provider/merge, docs) found defects behind
the 935-green suite — two new gate-integrity holes, two first-run functional
bugs, and default-config exposure. All fixed red-first. The audit also confirmed
the whole prior foundation holds (C1–C5, N1, N2, I1/I13, panel/merge fail-closed
guarantees, GC — all independently re-reproduced).

### Security

- **NEW-1 (High) — code-edit gate bypass via `..`.** The gate exempted any path
  *containing* `.agent`/`.claude` without resolving `..`, so `Edit
  .agent/../src/main.py` was exempted while the write landed on the real code
  file — a one-string defeat of "no code without an active task", on both the
  Claude hook and the codex `_is_management_path`. Both now lexically normalize
  the path (no fs/symlink resolution) before the exemption and the code-file test.
- **NEW-2 (Medium) — enforcing gate fail-open on an unwritable sessions dir.**
  `mkdir -p "$SESSION_DIR"` ran unguarded under `set -e`, so a full/read-only
  sessions dir aborted the hook with exit 1 (non-blocking) and the edit proceeded.
  Guarded (`2>/dev/null || true`); on failure the pointer simply doesn't exist,
  which reads as "no active task" (block).

### Fixed

- **`tasks freehand log` crashed on Python 3.10 (B2).** The reader parsed the
  `Z`-suffixed timestamp it writes with `datetime.fromisoformat()`, which rejects
  `Z` before 3.11 — so the feature was dead on Ubuntu 22.04 (a common host)
  despite the plugin's 3.10+ floor. Normalize `Z` → `+00:00`.
- **`tasks new light <name> <intent>` dropped the intent (B1).** `create_task`
  substituted intent only into the feature/quick placeholders, not the light
  template's. Added it.
- **`tasks --help` omitted the `light` task type (D2).** Now listed.
- **Parity hardening:** stop-hook reads its counters as integers (C5 parity), and
  `shared._own_session_id` is sanitized via the shared resolver (both flagged by
  the audit; neither exploitable).

### Changed

- **Shipped judge defaults are now all-Claude.** `default_judge` → `opus` and the
  default panel → `[opus, sonnet]` (was codex-default + a mixed panel). A
  Claude-first user's `tasks judge`/`panel-review` no longer exercises the codex/
  grok/pi adapter drift (I16/I17). Other vendors remain available via the
  `models.json` aliases, `.agent/models.json`, `--backend`, or `--models`.

### Docs

- Documented `panel_required_for` in the config `_doc` and reconciled the
  `--help` close text with the panel-always default (the policy is unchanged —
  every close needs a PASS panel under the seeded `"all"`).
- Fixed the `configuration.md` alias example (was an off-schema bare string the
  parser silently drops → the `[agent, model, [extras]]` schema); `/init` →
  `/playbook:init` and the pattern list in `commands/playbook.md`; a ghost
  `src/tasks/` path and the "Never batch" wording in `playbooks-README.md`; and
  the mind-map node-count contradiction. Refreshed the README audit baseline.

## [1.5.11] — 2026-08-16

Closes the two residuals the independent analyst pass found in 1.5.10
(`verification-report-1.5.10.md`) — both incomplete fixes from that batch, each
reproduced by hand and now fixed red-first.

### Security

- **The session-id sanitization now covers the codex resolver too (N1).** 1.5.10's
  C4 fix sanitized the bash and `tasks.core` resolvers but not
  `provider/codex_hooks.resolve_session_id`, which returned `PLAYBOOK_SESSION_ID`
  verbatim — reached on every codex user prompt and composing hook paths that are
  written (`counters`, turn-baseline, stop-marker). `PLAYBOOK_SESSION_ID=../tasks/…`
  escaped `sessions/` and wrote inside the task dir (a path-traversal write
  primitive; no `rm` on the codex side, so not the task-DB deletion C4 was, but the
  same vector). All three resolvers now share the whitelist, making the "one
  resolver contract" true. Not exposed on all-Claude deployments.
- **The code-edit gate resolves the pointer as a number, not a glob (N2).** 1.5.10's
  I2 hardening resolved `current_state` through `find -path "*/${TASK}-*/*"`, so a
  glob metacharacter (`current_state=*`) matched a real task and self-authorized a
  code edit. Task pointers are numeric: a non-digit pointer is now rejected before
  the glob in the task-gate (the auth decision), the stop/state-echo hooks, and the
  lifecycle close path (where `*` would otherwise close the wrong task).

### Deferred

- **N3 (Low) — non-UTF-8 task.md prose is normalized to U+FFFD on write-back.** I10's
  `errors="replace"` reads heal a corrupt (non-UTF-8) task.md to valid UTF-8 but lose
  the original non-ASCII bytes when a status/close/reopen write rewrites the file.
  `errors="surrogateescape"` would round-trip, but it relocates the crash to every
  *encode* site (print to stdout, `json`, subprocess argv) — the opposite of the
  crash-safety I10 exists to provide. Status/gates/structure are ASCII and preserved;
  only free-text in an already-corrupt file is affected. Kept as a documented
  tradeoff, consistent with the accepted `claude-md-merge` U+FFFD class.

## [1.5.10] — 2026-08-15

The fit-for-autonomy hardening batch. A fresh-eyes cross-vendor inspection (`verification-report-1.5.9.md`) found five CONFIRMED criticals plus a tier of Important defects that an 852-green suite missed — because they lived in the exact paths the suite did not execute. Every item here is a localized bug fix or a coverage hole; no new gates, no new seats. Each fix ships with a test that watched the bug fail first (for the criticals, the test is the report's own reproduction) plus a negative control.

### Security

- **Unsanitized session id reached `rm -rf` (C4).** `resolve_session_id` (bash + Python) returned `PLAYBOOK_SESSION_ID` verbatim, and `session-end-hook` runs `rm -rf "$AGENT_DIR/sessions/$SESSION_ID"` — so `PLAYBOOK_SESSION_ID=../tasks` + `{"reason":"logout"}` deleted the task database. Reachable both adversarially (a prompt-injected agent can `export` it; the docs propagate it via `BASH_ENV`) and accidentally (any `/` or `..`). Now sanitized in the one resolver every hook shares — accept only a safe single directory component (the canonical `pid-*` ids and the sanctioned `judge`), neutralize anything else to the derived pid — before any path/rm/env use.
- **Command injection via the per-session counter file (C5).** `state-echo-hook` computed `$(( ${TOOLS_VAL:-0} + 1 ))` on counter bytes read from `.agent/` (which the task-gate exempts from the code-edit gate), and bash arithmetic evaluates command substitution embedded in an operand — so `tools=x[$(touch PWNED)]` ran `touch`. Counters are now read through a strict integer coercion (`_safe_int`), never bash arithmetic on untrusted file bytes; the same class in `chat-log-hook`'s `get_next_id` is fixed too.
- **Enforcing gate + Stop hook failed OPEN on a malformed marker (I1).** A malformed `.agent/current_user` made `resolve_agent_dir` `exit 1`, which under `set -e` aborted the whole hook with a non-blocking exit 1 — so the code edit / the stop proceeded. Trivially reachable (`echo alice@evil > .agent/current_user`). Both enforcing hooks now FAIL CLOSED (block) instead of aborting-open, while still allowing `.agent`/`.claude` edits so the marker stays fixable. Reconciles the records claim that no surface falls back to shared-root state on a malformed marker.
- **The code-edit gate trusted any non-empty `current_state` (I2).** With `.agent/**` gate-exempt, an agent could `Write current_state=junk` then edit any code file. The pointer must now resolve to an existing `NNN-*/task.md`.
- **The judge Bash guard was dead code (I3).** The session-injection early-exit matched `tasks (work|…|new)` and exited before the judge guard, so a judge session could create/activate tasks. The guard now runs first.
- **NotebookEdit / MultiEdit bypassed the task gate (I13).** The PreToolUse matcher omitted both and `.ipynb` was absent from the code-file classifier. Matcher, classifier (claude + codex mirror), and Guard 1 now cover them.

### Fixed

- **`work done` on a non-resolving pointer faked "done", wiped the session, then crashed (C1).** The close path read the unbound `task_file` and ran the session-pointer wipe outside the `if matches:` guard, so a pointer whose `NNN-*` glob matched nothing printed a false "Task X done.", deleted every session dir pointing at it, never wrote `## Status`, then threw `UnboundLocalError`. Now resolves the pointer to a real task before any destructive step; if it does not resolve, fails loud and changes nothing. Also C1b: `_find_active_task` matched the numeric filter as a substring (`work 100` activated `1000-bar`) — now an exact `NNN-` prefix.
- **`prepare-merge` corrupted task-directory names < 100, and its dry-run lied (C2).** The renumber sliced by the unpadded number's width (`002-feat-two` → `302-feat-two`), and the preview used a different computation. One shared pad-preserving helper now backs both, so preview == action.
- **The mind-map integrity verifier failed open on an unbalanced fence (C3).** An unterminated ``` fence made everything after it invisible and the verifier reported clean; two stray backticks swallowed every `[N]` between them across newlines. Now fails CLOSED on an unbalanced fence (mirroring `mindmap._node_starts`), and the inline-code strip can't cross a newline.
- **`scripts/init` aborted mid-install (I4).** An unguarded `MERGE_OUT=$(python3 …)` let `set -e` kill init before the summary on a CLAUDE.md merge failure, and embedded-python calls interpolated paths as `'$PATH'` so a single-quote project path (`~/John's proj`) raised a SyntaxError → abort. The merge call is guarded and every path/command reaches embedded python via the environment.
- **`tasks doctor` exited 0 while printing failures (I5).** Now exits non-zero when any check FAILs (warnings stay advisory).
- **The merge-artifacts audit sweep failed open on an incomplete scan (I6).** `find … | grep .` took its status from grep, so a permission-denied `find` classified CLEAN. Rewritten to check find's own exit status: error → ERROR, matches → FINDINGS, nothing → CLEAN.
- **Non-atomic task.md writers contradicted the atomicity claim (I9).** Reopen, stub expansion, chat injection, and the freehand inserts used plain `write_text` (truncate-then-write, torn reads); they route through `_atomic_write` now.
- **The open/close path crashed on a non-UTF-8 task.md (I10).** One `0xE9` byte made a task unopenable and uncloseable (`UnicodeDecodeError` slipped past `except OSError`). task.md-family reads decode leniently.
- **`--reason` swallowed the next flag (I11).** `work done --reason --force` recorded "--force" as the reason; a `--`-prefixed token is no longer taken as the reason.
- **`_capture_recent_chat` was dead on modern chat entries (I12).** The header regex anchored the backtick host tag before the newline, but the producer appends a ` (provider/pid)` suffix since 1.4.3.
- **`hooks.json` timeouts were 5000 seconds = 83 minutes (I14).** A milliseconds-era leftover for the intended 5 s; all five set to 5.
- **JSONL/stream readers assumed every record is a dict (I18).** A `null`/`[]`/string line, or a `"message": null` record, raised AttributeError — and in the monitor's incremental reader the crash preceded the offset save, so it re-read the poison line forever and wedged permanently. The sensor and the vendor transcript readers now skip non-dict records.
- **Skill docs described surfaces that don't ship (I15, I19).** The judge skill's only mechanism (`Task`) is on init's `permissions.deny`; it now names the sanctioned `tasks plan-review`/`impl-review` path. The monitor skill documented `monitor.py`, `.pid`, `/monitor off`, and a `pids/<sid>/` layout — none of which exist; rewritten to the real `launch-monitor` + `bootstrap.sh` + `sensor.py` + flat `<agent-dir>/monitor/` interface.

### Tests

- **852 → 929.** The systemic fix that hid the C-tier: the six `*-fixture.sh` shell suites (session-GC parity, wrapper-lane, init, merge-doctor, merge-verify, gate-logging) are now wired into `unittest discover` via `test_shell_fixtures.py`, which shells out to each and skips cleanly when a needed binary is absent. Plus writer-side tests for the untested load-bearing writers the report named — the review findings sentinel and salvage log names pinned to literals (killing the P6-survivor mutation), the `merge_prep` renumber/ref-rewrite writers, the chat-log entry format — and negative controls extended to the verifiers themselves (C3/I6). Every critical and Important fix above carries a red-first reproduction test.
- Deferred with reason: I7 (mindmap-sync/ref-integrity `↗`-node disagreement — needs a canonical-semantics design pass), I8 (prepare-merge prose false-positives — the active danger C2 is fixed; narrowing the ref regex risks legitimate rewrites), I16/I17 (single-review adapter drift / pi availability — not localized; needs live vendor validation), I20 (monitor sensor partial-line / `--pid` — needs careful partial-record semantics).

## [1.5.9] — 2026-08-14

The structural release: `tasks/cli.py` — 4,868 lines, every command arm inside one 3,475-line `main()` — split into eight cohesive modules behind a dispatch-only entry point. A behavior-preserving refactor, executed leaf-first with the close path moved last, one commit per peel, suite green at every commit; design (`design-1.5.9.md`, fork owner's notebook) red-teamed and blind-judged (PASS-conditional; all five conditions built, including the judge's Critical catch below).

### Fixed

- **Both review arms crashed on a trimmed task.md (judge F1, Critical — live since 1.5.3, pre-existing).** Sibling arms' local `import re` statements made `re` a local of the whole `main()`, so the two bare `re.search` trim-notice sites (panel `_build_payload`; the single-review context build) raised UnboundLocalError whenever `select_task_context` actually trimmed an oversized task.md. Unreached in the field only because every task.md fit its budget. Found by the split's blind judge inspecting `main.__code__.co_cellvars` — proof that 837 green tests did not pin uncovered paths, and fixed BEFORE the split in its own commit so the move could not silently repair an untested crash. Regression test drives both arms through the real CLI with a ~165k task.md.
- **User-facing path hints name the RESOLVED lane (genesis cosmetic, F-class widened).** `tasks list` on a multi-user repo printed `Task files: .agent/tasks/<name>/task.md` — a path that does not exist there. All 11 sites that named a lane-resident path with the single-user literal (the list hint, both "No .agent/tasks/ directory found" sites, the chat_log/bash_history not-found messages in context/timeline/tagger/tag/log, freehand log) now print the path the command actually resolved. Single-user output is byte-unchanged.
- **mindmap-optimize's abandoned-task scan command works in real projects.** Step 5's only command was the dev-repo-shaped `PYTHONPATH=src python3.12 -m tasks.cli list --pending`, which fails everywhere the skill actually runs; found by driving the skill end-to-end against a real map. Now the installed-wrapper form with a dev fallback.

### Changed

- **cli.py is dispatch-only: 168 lines.** Command bodies moved verbatim to `tasks/lifecycle.py` (work/close/new/blocked/parked/freehand), `tasks/review.py` (panel + single-judge + tamper machinery), `tasks/history.py` (context/intent/timeline/tagger/tag/retro/log), `tasks/diagnostics.py` (doctor/audit), `tasks/project_setup.py` (init/bootstrap), `tasks/mindmap.py` (map parsing/trim + mindmap-sync), `tasks/merge_prep.py` (prepare-merge/merge-doctor), with shared helpers in `tasks/shared.py` (root discovery, THE session-liveness policy, merge-verify loading). Every module opens with a one-paragraph boundary header; import direction is one-way (shared < mindmap < command modules < cli). `python3 -m tasks.cli` and the shipped wrapper are unchanged; `--help`, `list`, and `doctor` output byte-compared against the pre-split tree.
- **Doctor check #7 (encoding= on write_text/read_text) scans the whole tasks package** instead of resolving `[cli.py, core.py]` via `sys.modules[__name__]` — the old resolution would have silently shrunk the scan once the arms moved. Output identical today (every module scans clean); a planted-unencoded-call negative control pins that the widened check can still fail.

### Verified live (no code change needed)

- **The lane rename/rename rescue is field-closed** — the last suite-only merge choreography. Scratch two-user repos drove both contamination shapes live: the marker variant AND the doctrine's silent variant (content-merge succeeds with zero conflict markers, both lanes contaminated). `tasks merge-doctor` caught every instance with per-file attribution in both inspection modes (mid-merge and post-merge-commit); staging didn't launder; the Step 4 reset-to-own-branch rescue converged to SAFE TO CONTINUE, idempotent; final lane files byte-identical to their own branches.
- **`/playbook:intent` and mindmap-optimize ran as full LLM passes** (installed CLI, StrataDB): intent produced 4/4 grounded blind extractions (chat layer via the F2 timestamp-window fallback, dirty-worktree provenance honestly flagged) with the reconciliation seams workable as written; the mindmap 1.5.6 claim-consistency lens caught a real, new contradiction on the live map ([8] "Next roadmap slice is v2 durability" vs [1]/[10] "v3 complete"). F23's single-map degrade also fired correctly through the installed CLI.

### Tests

- 837 → **852**: the dispatch pin (COMMANDS/source parity + per-command baseline markers, exit codes, and no-traceback smoke through the real entry — an orphaned or miswired arm fails loudly; mutation-checked red), the review trim-path regression (watched RED at the real UnboundLocalError), the doctor encoding-scan pair (PASS + planted-failure negative control), and the lane-aware hint matrix (multi-user lane paths + single-user byte-unchanged controls).

## [1.5.8] — 2026-08-14

The genesis release: findings from the first full-lifecycle gauntlet on a NON-Python project (genesis-ts, TypeScript/node:test) — start on an empty repo, through real panels, F18-in-anger, blocked lifecycle, retro, the merge skill, and multi-user lanes.

### Fixed

- **`mindmap-sync` no longer hard-errors on a single-map project (F23).** The merge skill's Step 6 mandates it, but a young project has no `MIND_MAP_OVERFLOW.md` yet — the command now prints a clean note and exits 0 (the same graceful degrade `ref-integrity.py` ships); a missing `MIND_MAP.md` still errors.
- **Monitor state churn is not judge tampering (F22).** `_detect_tamper` skips `.agent/monitor/` and `.agent/<user>/monitor/` (the monitor is a sanctioned concurrent writer, OS-contained to that dir); the snapshot uses `-uall` so untracked files are named individually. A non-monitor `.agent` file still flags.

### Verified live in the genesis gauntlet (no code change needed)

- init on an empty zero-commit repo; chat-log-hook; `tasks tag` + span-based `context` (first runs); the tamper guard's first live firing (correct, on a real mid-review tree change); a real codex judge AND a real 4-seat panel on TypeScript (findings quality held cross-stack; judges caught fabricated completion traces twice); verify-at-close + merge_verify on a `node --test` contract; F18 block→refusal→recorded-acceptance in anger; blocked lifecycle through the real Stop hook; retro generation; the merge skill end-to-end (merge-doctor first live run, semantic map merge, ref-integrity, code identity); multi-user lane resolution + isolation.

### Tests

- 833 → **837**: tamper monitor-churn + non-monitor control, sync no-overflow degrade + missing-map control.

## [1.5.7] — 2026-08-14

The batch-6 release: three field findings from the first 1.5.6 workload (StrataDB task 012, v3 concurrency) — one of them owner-found in the monitor's first attached run.

### Fixed

- **The monitor binds to the session's OWN transcript (F19, owner-found).** It used to resolve its target as "newest `.jsonl` by mtime at bootstrap moment" — unlinked to the pid it announced — and in batch 6 it tailed a stale conversation's EOF for 40 minutes while the real session streamed megabytes into other files. Now: the session-start and state-echo hooks record the hook payload's `transcript_path` into `.agent/sessions/<id>/transcript_path` (refreshed every tool call, so a compaction rollover moves the pointer); bootstrap prefers the pointer ("session-bound" in the briefing) and falls back to the mtime guess only with a loud warning; the sensor takes `--pointer-file` and re-resolves every invocation, resetting its offset when the pointer moves to a different file (the offset file is now path-aware; legacy int-only files still parse).

### Changed

- **The born-checked block shows the closest open gate to restore (F20).** The batch-6 agent truncated a parenthetical while appending an outcome and "had to eyeball what I'd dropped" across 3 retries. Each born-checked line now carries the nearest OPEN original underneath, so restoration is copy-paste; a fabricated gate with no near-miss gets no hint.
- **Activation nudges the parked-consumption marker (F21).** Task 012 consumed task 010's parked item — the designed pickup — but nothing at the consumption moment taught `[promoted → NNN]`, so the source entry still read open. `tasks work <N>` now prints one line when open parked items exist in earlier tasks; silent when all are resolved.

### Tests

- 821 → **833**: transcript binding (hooks record/refuse-newline, bootstrap pointer-beats-decoy + loud fallback + pointer-following WAIT command, sensor pointer override + switch-resets-offset + stable-pointer persistence control), born-checked closest-original + no-hint control, parked-pickup nudge + all-resolved silence control. All new behaviors watched red or mutation-checked.

## [1.5.6] — 2026-08-14

The batch-5 release: every item traces to the first real 1.5.5 workload (StrataDB task 011, the v3 migration) or the gauntlet-155 live pass — see the fork owner's lab notebook.

### Added

- **The irreversible freshness gate (F18; design → red-team → blind judge, all five findings built).** A close whose `## Risk` is `irreversible`, whose panel evidence is required by `panel_required_for`, and whose newest impl round is a quorum-PASS whose `Tree-state` stamp no longer matches the tree, now BLOCKS: the panel's verdict predates the code being closed. Two exits, both durable — re-run the impl panel, or `tasks work done --stale-panel-ok --reason "..."` (narrow override, suppresses only this gate, reason lands in the receipt). Batch 4 closed exactly this way with the delta judgment living only in prose; batch 5's agent re-panelled voluntarily and caught two CRITICALs its own fixes had introduced, then asked for this gate in its journal.
- **Panel freshness is part of the close receipt for every close** with an impl round: `FRESH`, `STALE (code changed after newest impl panel)` (+ `accepted: "..."` when overridden), or `no stamp recorded` (a missing stamp is recorded, not silently treated as legacy — the judge found that was the one zero-record bypass). Closes F17's console-only gap with an artifact.
- **`fingerprint_exclude` config** (git pathspecs, e.g. `"journal/"`): owner-declared bookkeeping written after the last panel by standing gates no longer reads as a stale tree. Malformed entries are skipped loudly. Commit the file — stamp and close must agree across clones.
- **`--print-argv` and `--ro-project` on `provider.sandbox`**: inspectable containment argv, and a contained-observer mode (project read-only, only `--rw` paths writable project-side).

### Fixed

- **The monitor runs on Linux.** `launch-monitor` hard-exec'd the macOS sandbox binary with a hand-rolled seatbelt profile — no Linux branch, owner-found on first launch attempt. It now delegates containment to `provider.sandbox` (Darwin seatbelt / Linux bwrap, the 1.5.3 bind-order lesson encoded once): project read-only, `<agent-dir>/monitor/` the only project-side writable. Hook suppression moved from the `--settings '{}'` shim to `claude --safe-mode` (T136: settings overrides cannot suppress plugin-registered hooks). Live-verified under bwrap: project write denied, monitor dir writable, reads intact.
- **`tree_state_fingerprint` was blind to edits in untracked files** (judge C1 on the F18 design, verified empirically): porcelain names an untracked file without its content and `git diff HEAD` covers tracked paths only — and new-file work is exactly where batches 4 and 5 put their post-panel fixes. Untracked content is now hashed explicitly (`-uall` enumeration + per-file sha256). Fingerprint values change across this upgrade: an old round's stamp reads STALE once and self-heals at the next panel.
- **`tasks new` accepts `--stub` anywhere** in the argument list. A trailing `--stub` used to be silently swallowed into the task's Intent text and a full template was created instead of a stub (gauntlet-155 wart).
- **The born-checked block message teaches the rewrite case.** Batch 5's agent burned ~4 retries inferring that rewriting a gate's text while checking it is what "born-checked" means; the message now names the cause and the fix (keep the original text, append the outcome).

### Changed

- **`mindmap-optimize` gains a claim-consistency lens**: cross-node contradictions (an overview node saying "still ahead" while the owning node says "shipped" — observed live in batch 5) are flagged with both quotes and the owning node named, with the single-home fix taught. The report gains a `Claim Contradictions` section.

### Tests

- 802 → **821**: batch-guard rewrite-case message, `--stub` position matrix, mindmap lens surface pins (mutation-checked), launch-monitor containment (argv shape under bwrap + launcher delegation pins), and the F18 suite (fingerprint coverage incl. untracked/dir/exclude/malformed, pure gate matrix with negative controls, end-to-end close matrix: block / override refused bare / override recorded / FRESH / advisory-only risks / no-policy control / FAIL-round and replan fall-through / no-stamp clause / --force attribution). Gate mutation-checked red-first.

## [1.5.5] — 2026-08-13

The field-backlog release: every item below carries evidence from the StrataDB stress test (batches 1–4) or the 1.5.4 full-surface gauntlet — see the fork owner's lab notebook.

### Tests

- **The monitor skill has tests** (it shipped with zero). `tests/test_monitor_sensor.py` (23) pins the mechanical seams: sensor JSONL extraction (noise/isMeta filters, malformed-line resilience, thinking markers, turn boundaries), BYTE-offset arithmetic under real multi-byte UTF-8 (the fixture now writes raw UTF-8 like the real session files — an all-ASCII fixture was proven toothless by mutation), incremental resume, `wait_once` (cold start seeds at EOF, turn-end flush, stall flush for crashed agents, dead-pid exit, nothing-happened → nothing reported), the `monitor-nudge.sh` delivery hook (pending nudge consumed/emitted/logged; **no nudge → no output**; the monitor's own session never eats its own nudge; malformed lane markers deliver nothing), and `bootstrap.sh` guards (no project dir / no session id / shell-metacharacter SESSION_ID all refused; happy path emits the COMMANDS briefing and seeds the offset at EOF). What the monitor *decides* — nudge or silence, judgment quality — is LLM work and is deliberately not asserted; the tests cover everything that feeds and delivers those decisions.

### Verified

- **The close-time tree-state freshness advisory fires** (F17 — flagged unverifiable in the field because it is console-only). Reproduced end-to-end under the field scenario's exact conditions (impl round stamped, code edited post-panel, close): the advisory prints; a matching fingerprint stays silent. The 1.5.3 suite never exercised the close path — `tests/test_freshness_advisory.py` now pins mismatch-fires, match-silent, and no-impl-round-silent.

### Added

- **The `light` task shape** (F14 — a ~20-line doc note dragged the 32-gate Build template in the field, because `quick` has no review gates and assertive work must be reviewed; design blind-judge-reviewed, first draft FAILED and was rebuilt to the verdict). `tasks new light <name>`: ~6 gates — risk classified FIRST with a one-line written why, three work gates, review routed by risk, one pre-review gate. **Ceremony is compressed; review is not:** an `assertive`/`irreversible` light task cannot close without implementation-review evidence — enforced close-side (`close_decision`), template-independent, and proven by negative controls including one the judge demanded: the rendered template with EVERY gate checked mints no review evidence (the first draft's gate wording did, via substring matching — caught blind). Selection rule on both stickers: `quick` is declared-reversible trivia only; docs/claims/data/publishing → `light` or heavier. Docs recommend `panel_required_for: ["assertive","irreversible"]` for adopters — panel evidence is structural, a checkbox cannot mint it.

### Fixed (found by the F14 blind judge — pre-existing, every task shape)

- **`tasks work <N>` can no longer close the previous task behind the evidence contract's back.** The switch path auto-closed a fully-gated previous task by writing `done` directly — no risk check, no review evidence, no verify contract, no receipt: a policy-free second close path that defeated the 1.5.0 contract for every shape (and made "finish and start the next thing" the natural way to skip review). The switch now bounces to `tasks work done` (which has always been the real close), and `--force` switches away leaving the task honestly open — never silently done. `tasks work done` is again the only writer of `done`, the 1.4.7 principle. The codex stop-hook message that advertised auto-close is updated (mirror re-synced).
- **Closing with `## Risk` unclassified now warns loudly** instead of failing open in silence — `unclassified` is not high-consequence, so the risk-keyed review bar was never evaluated for such closes (and a malformed one-line `## Risk: assertive` parses as unclassified). A warning rather than a block: every pre-1.5.0 task is unclassified, and panel-always projects already hold every close to panel evidence regardless of risk.

- **`init` writes CLAUDE.md and .gitignore mechanically** (F15 — gauntlet: those files were the AGENT half of /playbook:init, so the doctrine they carry held only if the agent performed it; it did, but a guarantee beats a habit). New `scripts/claude-md-merge.py`, run by `scripts/init`: CLAUDE.md is created from the template, or **merged, never clobbered** — template-owned sections update in place, everything else (a seeded pointer, custom sections, the project title) survives byte-for-byte, re-runs are idempotent; .gitignore gains one marker-guarded block of playbook runtime-state entries (sessions, chat log + counters, bash history, `current_user`, `models.json` — root and per-user lanes both), appended exactly once, existing content untouched. `/playbook:init`'s agent step shrinks to reviewing the merge and adding what the template cannot know. Negative control in the tests: a seeded CLAUDE.md pointer survives a re-init.
- **Standing gates** (F8 — the journal gate was hand-relocated below Pre-review VERBATIM on two consecutive field tasks; a gate a project wants on every task should come from generation, not agent memory). `standing_gates` in `.agent/config.json`: a list of `{title, text}` entries appended in declared order as the **final gates** of every generated task — base templates, quick, custom playbooks, and stub expansion alike. `{{NNN}}` substitutes the task number (`journal/{{NNN}}.md`). Opt-in: absent means generation is byte-identical to before, and `init` seeds none. Malformed entries and title collisions are skipped loudly; title/text are collapsed to one line so config can never mint a phantom section or gate (the #09 disease, closed at the config door too). Documented in `docs/configuration.md`.

### Changed

- **The batch-close guard now trades ceremony for evidence** (F1 — 6/6 field journals called the one-checkbox-per-edit tax the workflow's worst friction, sharpest where one logical step spans several gates; design blind-judge-reviewed, verdict recorded in the fork owner's notebook). Guard 0.5 v2 (`scripts/gate-batch-check.py`, called by `task-gate-hook`): closing 2–5 ALREADY-DONE gates in one write is now allowed **only when every newly-checked line carries its own outcome note** (≥ 8 non-whitespace chars appended to the gate text — "→ 283 green", or a pointer like "— see Round 2 Result" when the outcome lives under the gate). What still cannot happen, mechanically: a **bare batch tick blocks** (the old guard merely warned at 2 — tightened); **6+ blocks even fully annotated**; a **born-checked line** (a gate minted already closed) blocks the batch; **unchecking cannot launder** the batch size (tiers key on the newly-checked count, not the raw delta); and **two batch closes with no tool call between them block the second** — the end-of-task two-writes-of-5 pattern is dead. Singles keep their old freedom. Doctrine text updated uniformly across every surface (sticker, base templates, CLAUDE.md, codex/agy onboarding, activation briefing): batch permission is hook-gated; where no hook runs the same sentence says keep to one gate at a time. The guard fails open on its own errors — only an explicit block exits 2.

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
