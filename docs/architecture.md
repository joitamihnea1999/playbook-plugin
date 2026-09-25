# Architecture

How the plugin is put together, and how the enforcement actually works.

## Layout

The plugin (`plugins/playbook/` in this repo) has four user-visible parts plus two engine directories:

- `commands/` — seven `/playbook:*` slash commands (markdown the agent executes as instructions).
- `skills/` — six harness-discoverable skill bundles, each with a `SKILL.md` (playbook patterns, judge, monitor, merge, stack, testing), plus `skills/tasks/` which holds the canonical task template the `new` command copies (not a discoverable skill).
- `hooks/hooks.json` — the lifecycle hook registrations (below).
- `scripts/` — executable entry points: the `tasks` dispatcher, hook scripts, `sandbox`, `monitor`, `init`, the `playbook-*` provider launchers.
- `tasks/` — the Python package behind the `tasks` CLI (dispatcher sets `PYTHONPATH` here).
- `provider/` — provider adapters and judge-dispatch machinery ([providers](providers.md)).

Everything is plain files — bash entry points, Python 3.10+ stdlib, markdown as the runtime language. No build step, no dependencies. Python 3.10 is the declared floor everywhere (shipped modules use 3.10-only `match` syntax): every entry point refuses an older interpreter up front, and `tasks doctor` diagnoses it.

## Hooks & enforcement

Hooks enforce the structure at the OS level, because warnings don't stick — blocking does:

| Hook | What it does |
|---|---|
| `SessionStart` | Persists the session id: writes `export PLAYBOOK_SESSION_ID=<id>` into `CLAUDE_ENV_FILE` so every later Bash call this session sees it, giving each agent its own `.agent/sessions/<id>/`. It also provisions the `.claude/bin/` wrappers and sweeps dead session dirs — but it does **not** print orientation. Loading the mind map + pending tasks + CLI reference is the separate `tasks bootstrap` command, which the agent runs as its first action. |
| `PreToolUse` (matcher `Edit\|Write\|MultiEdit\|NotebookEdit\|search_replace\|write\|Bash\|Shell\|StrReplace\|run_terminal_command`) | The **task gate**: BLOCKS code edits when no task is active. Covers Claude's `MultiEdit`/`NotebookEdit` (they edit code too) alongside `Edit`/`Write`; grok names (`write`, `search_replace`, `run_terminal_command`, `Shell`, `StrReplace`) map to Claude Edit/Write/Bash via the normalizer — the same gate path runs under every provider in code, but **supported enforcement is Claude-only** (grok/codex experimental, antigravity/pi experimental everywhere; owner decision 2026-08-21 — see the [provider support matrix](providers.md#support-matrix)). |
| `PreToolUse` on shell tools (`command-guard-hook`) | The **destructive-command interlock**: BLOCKS a high-blast/irreversible command (`rm -rf` a dangerous path, `git push --force`, `git reset --hard`, `curl\|sh`, a DB `DROP`/`TRUNCATE`) until acknowledged (run inside an `irreversible`-classified task, or the operator sets `PLAYBOOK_ALLOW_DANGEROUS=1` in the hook's environment). Conservative, and a command position is found THROUGH wrappers and their options (task 077): `sudo -u root …`, `timeout 5 …`, `chrt -f 99 …`, `stdbuf -o0 …`, `env -S '<cmd>'` and `curl … | nice -n 19 bash` reach the same rules as their bare forms, while a wrapper's query mode (`sudo --version …`, `command -v …`) runs nothing and stays allowed, and an unknown value-taking option under-blocks by design. A generic `cat evil.sh | sh` is allowed by recorded decision — only a downloader piped into an interpreter blocks; the stricter rule is a one-line `dangerous_commands` opt-in. (command-position matches, and the two whole-command rules skip only INERT data — a `cat`/`tee` heredoc body written to a plain file, and an `echo`/`printf` line redirected to a plain file with no pipe or expansion on it — so `echo "rm -rf /"` and a `cat` fixture about a piped installer are fine while `bash <<EOF`, `echo … | sh`, `tee >(sh)`, `$(…)` and interpreter heredocs are still seen; the command-position rules are never masked, so a heredoc body line `rm -rf /` still blocks), fails OPEN on any internal error, config-extensible (`dangerous_commands`) / disable (`command_guard: false`). Wired on three providers (Claude via `hooks.json`, grok via its always-trusted enforcement, codex via a `PreToolUse ^exec_command$` hook), but supported enforcement is Claude-only — the grok and codex interlock paths are experimental. |
| `UserPromptSubmit` | Appends every user message to `.agent/chat_log.md` (timestamped, agent-tagged — feeds task attribution and `tasks log`). A prompt that *begins* with a harness marker (`<task-notification>`, `<local-command-caveat>`, `<command-name>`, `[SYSTEM NOTIFICATION`) is a harness event, not the user's words, and is not logged. |
| `PostToolUse` | Echoes gate state after every tool call, keeping the current gate in the agent's face. |
| `Stop` / `SessionEnd` | Finalize session state. SessionEnd removes the session directory only when the process is really exiting — `/clear` keeps it, because the same process continues and the active task has to survive it. |

Each enforcement decision is also appended best-effort as one JSON line to `.agent/<lane>/journal/enforcement.jsonl` — its `decision` field is one of `{allow, block, record}`: task-gate writes `allow`/`block`, command-guard/stop-hook/batch-close write `block`, and a close writes a `record` logging the verify bar it ran (a forensic record, not a gate allow/block). A second family on the same journal records **review spend** — one `hook:"review"` `record` per judge invocation (each panel seat, the single judge, the tail-cert judge), for token/effort attribution. Both are log-only and by tested construction never alter or block a decision (guarantees `PB-ENFORCEMENT-JOURNAL`, `PB-REVIEW-SPEND-JOURNAL`); the reader (playbook-lens) is a separate repo, none ships here. Record shapes: [enforcement journal](enforcement-journal.md).

The task gate runs two guards before the no-active-task check, both on edits to the active task's own `task.md`:

- **Guard 0 — manual-creation block.** A `Write` that would create a *new* `.agent/**/tasks/<N>-…/task.md` is blocked; only `tasks new` mints task files (it owns templates and numbering). Editing an existing task.md is fine.
- **Guard 0.5 — annotated batch-close.** Closing several ALREADY-DONE gates in one write is allowed only when each newly-checked line carries its own outcome note. The logic lives in `gate-batch-check.py` (shared and unit-tested; the hook delegates to it): one gate (n ≤ 1) is always allowed; a batch of 2–5 is allowed only if no line is *born-checked* (checked text with no matching open original — usually a rewritten gate) and every closed line appends ≥ 8 non-whitespace characters of outcome (`→ 283 green` passes, `— done` does not, a pointer like `— see Round 2 Result` is the sanctioned idiom); 6+ blocks even fully annotated; and a second batch with no intervening tool call blocks (kills end-of-task ticking). The helper **fails OPEN** on any internal error — only an explicit block exits non-zero — so a crashed guard never bricks the session.

The no-active-task check refuses a code edit when the session pointer is junk, stale, glob-bearing, or names a done task — and, since 1.5.45, when it names a `blocked` task (paused on a decision that is not the agent's); the refusal names `tasks work <N>` as the resume. On shell commands the gate also refuses creating a task directory by hand (task directories come from `tasks new`). Since 1.5.45 that rule applies only inside the project: a single, simple `mkdir` of a literal absolute path outside it — a test fixture in a temp dir — goes through, judged by path and through the filesystem (symlinks, case-insensitive disks), while a compound command, a relative path, `~`, a variable, a glob or brace, or an error in the check is still refused. Since task 085 the rule is judged on what the shell will actually create: ANSI-C `$'…'` quoting is decoded, quotes and backslashes are removed and brace expansions are expanded (comma lists, nesting, `a..b` sequences), for the command word as well as the path — so `.ag'ent/tasks'`, `.ag\ent/tasks`, `ta$'\x73'ks`, `ta{sk,zz}s`, `ta{r..t}ks`, `m\kdir` or `m'kdir'` no longer slip past a raw-text match (a glob needs no help: it only matches paths that already exist). The name is matched against the real task-directory pattern, case-insensitively, so `.agentic/tasks` passes and `.AGENT/TASKS` (the live tree on a case-insensitive disk) does not; a word that still holds a variable or a glob is judged strictly only when it also shows part of the name, so `mkdir -p "$D/build"` passes. An expansion larger than 512 spellings is judged as written rather than enumerated (so `touch f{1..1000}` passes; a task directory hidden inside a brace list that large is not seen), while a `mkdir` that cannot name a task directory (`mkdir -p "$D/build"`) is not examined further. The whole command is judged, not the `mkdir` line, so a path assigned on one line and created on the next is refused too; the cost is that a multi-line command that merely mentions `mkdir` and a task directory on different lines (prose in a heredoc) is also refused — write such text from a file. The guard catches mistakes; it is not a boundary: a globbed or computed command name, a path built entirely from variables, and other ways of creating a directory (`install -d`, `cp -r`, an interpreter) are outside it — pinned by `test_the_bound_a_globbed_command_name`.

`/playbook:init` additionally writes a **deny-list** into the project's `.claude/settings.json` blocking `TodoWrite`, `Task`, and `EnterPlanMode` — those would compete with task.md as the source of truth. If those tools suddenly error in a playbook project, that's why.

A `bash-log` shell integration records commands into `.agent/bash_history`, so terminal work is auditable alongside the chat log. The bash logger (sourced through `BASH_ENV`) writes each distinct command text once per shell process and directory — a loop body is one line, not one per iteration; a `tasks … work|new` command is logged every time, since task windows are built from those lines — skips a script named `statusline` and any shell started with `PLAYBOOK_NO_BASHLOG=1`, and rotates a history past 50 MB to `bash_history.archived-<date>-<pid>`, copying the `tasks work`/`tasks new` lines into the fresh file. The line format is unchanged. The installed copy at `~/.claude/bash-log.sh` only picks this up when `/playbook:init` is re-run (see *Upgrading an existing install* below).

## Per-user lanes

On a repo shared by several people (or several workstations), agent runtime state is namespaced per user so nobody tramples anyone else's sessions:

- **Legacy / single-user** — no marker, everything lives under `.agent/`.
- **Multi-user** — `.agent/current_user` names the lane, and runtime state lives under `.agent/<user>/`: `tasks/`, `sessions/`, `playbooks/`, `monitor/`, `chat_log.md`, `bash_history`.

Two files stay at the `.agent/` root by design and are **not** lane-scoped: `config.json` (shared repo policy — `merge_verify` only works if every clone sees the same declaration) and `models.json` (per-clone judge pins).

**Session directories.** Each session gets `.agent/<lane>/sessions/pid-<PID>/`, holding `current_state` (which task is active) and `counters`. Dead ones are reclaimed by process liveness (`kill -0`), never by the age of `current_state`: that file is written once at activation, so its timestamp records when the task started, not whether the session still lives. Until v1.4.7 the SessionStart sweep used that timestamp and deleted the pointer of any session more than 24h into a task — surfacing as a sudden `No active task` and blocked edits mid-task.

Every surface that reads or writes runtime state resolves the lane — hooks, the `tasks` CLI, the `playbook-*` launchers, the codex hooks, the monitor and its nudge hook, and the shell command loggers. Four rules keep that consistent:

- **The marker is exactly one line.** A trailing CR is stripped (CRLF markers work), surrounding whitespace is ignored, and a missing final newline is fine — but a *second* content line is invalid, not "use the first one". Otherwise `alice\n../evil` would resolve to lane `alice` in the shell readers while Python rejected the same file.
- `.agent/current_user` is **gitignored and install-local**, so it never arrives with a clone. On a fresh clone of a multi-user repo — lanes present, marker absent — nothing is allowed to invent a lane. Surfaces you invoke directly (`tasks new`, `tasks init`, `/playbook:init`, the `playbook-*` launchers, the monitor) **fail loud**; hooks, which must never take a session down, **skip quietly** and `session-start-hook` prints a warning. Enforcement still fails closed: with no knowable lane there is no active task, so edits are blocked. Fix: `echo '<your-username>' > .agent/current_user`.
- A **malformed** marker is never treated as "use the root" — not for writes and not for reads. State-creating surfaces refuse; the shell loggers and the nudge hook skip silently; the provider adapters report *no active task* rather than falling back to root state (a stale root task must not be able to satisfy the gate); the Codex hooks apply their per-event policy — PreToolUse fails closed, the rest fail open.
- A repo that has *both* root `.agent/tasks/` and per-user lanes is a legitimate mixed layout — root is itself a lane — and is left alone.

**Upgrading an existing install:** two files are *copies* that live outside the plugin — `.claude/hooks/monitor-nudge.sh` (per project) and `~/.claude/bash-log.{sh,zsh}` (per machine). They keep their old contents until you re-run `/playbook:init`. A `bash-log.sh` from before v1.4.6 **silently disables gate logging entirely** (it could kill any `set -e` hook); an install predating lane support logs shell history to the root instead of your lane and doesn't deliver monitor nudges on a multi-user repo. If your gate log stopped for no apparent reason, re-run `/playbook:init`.

**On zsh hosts specifically:** installs initialised before v1.4.7 never received `~/.claude/bash-log.sh` at all — `init` deployed only the zsh variant while still pointing `BASH_ENV` at the bash one, so Claude Code's own Bash tool (always `/bin/bash`, whatever your `$SHELL` is) sourced a file that did not exist and logged nothing. Re-running `/playbook:init` deploys it and heals the dangling reference.

## Task system

A task is a directory under `.agent/tasks/<N>-<type>-<name>/` (or `.agent/<user>/tasks/…`) whose `task.md` is both the plan and the execution trace: Design Phase gates (understand → structure → reflect → verify) → judge review → Work Plan gates → implementation review → pre-review. State lives on disk, keyed by a PID-based session ID that works across providers — which is why tasks survive context compaction and session restarts, and why two agents can hand a task off through the file alone.

The final pre-review gate asks for the mind map to be updated by editing the **owning subsystem node in place** — a new node only for a genuinely new subsystem, never one node per task. Without that rule a long-lived map grows into an append-only changelog of tasks; with it, what you learned lands where the next reader will actually look for it.

## The monitor

A second Claude process that watches the front agent's session transcript incrementally and posts nudges through a hook when the trajectory goes wrong. Separate context window — it judges from outside, without the front agent's anchoring. Components: `.claude/bin/monitor` (wrapper), the plugin's `scripts/monitor` + `monitor-lib/`, and per-project rules under `.agent/monitor/` — or `.agent/<user>/monitor/` on a multi-user repo, where each user gets their own monitor state (scaffolded by init).

## The sandbox

`.claude/bin/sandbox` runs the agent with `--dangerously-skip-permissions` inside OS-level containment: **macOS seatbelt** or **Linux bubblewrap** with deny-write-by-default. This is **write** containment, not a read or network jail — "read-only" here means writes are denied, nothing more. The whole filesystem stays *readable* (bubblewrap mounts `/` read-only, it does not hide it), and the network stays reachable **by design**. What's enforced at the kernel level is: the project directory is writable, `.git` is read-only (history can't be mangled), and writes *outside* the project are denied — reads and network access are not. Network isolation exists only as an explicit opt-in (`bin/sandbox --no-network`, which adds bubblewrap's `--unshare-net`; **Linux/bwrap only** — it fails loudly on macOS seatbelt and Windows rather than pretend), and it is never used for judges, which must reach model APIs. One honest caveat: when no containment primitive is available (neither `sandbox-exec` nor `bwrap`, or nesting inside a foreign sandbox forbids it — and **Windows has neither backend at all**), the agent currently runs with bypass flags and **no** kernel containment — check your platform has one of the two before relying on the blast-radius guarantee. Pairs with the task system for the "two agents, one task" pattern: orchestrator outside, worker inside, task.md as the handoff.

## Judges

Blind by construction: a judge gets the repo but not your conversation, so it can't anchor to whatever was already agreed in chat. Single judge (`plan-review` / `impl-review`) writes findings into the task.md; the panel (`panel-review`) fans out to every seat in `models.json` in parallel and writes `judge.md`. Judge output is triaged, not obeyed — the task template's review gates require an accept/park/reject decision per finding.

**judge.md round format and retention.** Each `panel-review` run prepends one round to `judge.md`, newest first. A round starts at a `# Panel Plan Review` or `# Panel Impl Review` heading and runs to the next such heading (or end of file); the close gate reads only the newest round's `**PANEL VERDICT: …**`. `judge.md` retains the newest `JUDGE_MD_MAX_ROUNDS` (5) rounds — the review-read budget. Since v1.5.41 any older round that overflows that window is **archived** (its round content preserved as UTF-8 text) to a sibling `judge-archive.md` (newest first, never deleted — the same "moving history is not deleting it" contract as `task-archive.md`), and `judge.md` carries a one-line pointer to it. Earlier releases dropped the overflow with only a "…full history is in git" note, which was unreliable (judge.md is rewritten in place and committed at most once per task, so intra-session rounds were lost); the archive fixes that. **The true panel-round count for a task is the machine-written round headings in `judge.md` plus those in `judge-archive.md`.** Note two edges: (1) the 5-round cap on `judge.md` holds only when the archive write *succeeds* — if archiving fails, *this* call's overflow is kept untrimmed in `judge.md` (it may exceed 5) rather than dropped, while any rounds already moved out in earlier successful archives stay in `judge-archive.md`; no paid round is lost, and the true count is still the headings in **both** files (never `judge.md` alone); (2) the count assumes judges don't emit a `# Panel … Review` line at column 0 inside their findings (see the lens-v0.2 caveat below). (Caveat, tracked for a lens-v0.2 hardening: the heading is matched textually at column 0, so a judge that itself emits a `# Panel … Review` line at the start of a line inside its findings would be miscounted as an extra round; the round writer should grow an unambiguous machine-generated delimiter/ID that both retention and any counter key on.)

Since v1.4.3 the judge runs write-denied: the project is mounted no-write, so a judge physically cannot edit the repo or task.md. It is not otherwise isolated — it can still read the filesystem and reach the network (which it needs, to call model APIs); "read-only" here means writes are denied, nothing more. Because that OS containment is unavailable on some platforms (**Windows has no seatbelt/bwrap backend at all**, and nested sandboxes forbid it), a working-tree tamper guard backs it up — the review paths snapshot git status + the task.md hash before and after, and if a judge changed anything the review is saved with a loud TAMPER banner, ingestion is refused, and the run exits non-zero.

**What the guard sees, and what a degraded guard means (v1.5.45).** Its scope is
the outer tree's porcelain state, the content hash of every dirty/untracked file,
`task.md`'s own hash, the task directory's git-independent identity (realpath +
inode + symlink-ness, so a judge cannot swap it for a symlink and redirect the
trusted parent's later writes), and — when `code_roots` is set — each nested
repo's state plus its resolved identity. Dirty files are opened at the **git
toplevel** and named **project-relative**, so a project living in a subdirectory
of a larger repo is guarded correctly (before v1.5.45 those paths were joined
onto the project root, which made content-only edits invisible and `monitor/`
churn a false alarm).

Findings split in two. A **mutation** — a porcelain or content-hash change, a
task.md rewrite/deletion, a task-dir or code-root identity change, a `.git` that
disappeared, or an errored guard — still prints the loud TAMPER banner, refuses
ingestion and exits non-zero. A **caution** is the guard telling you it could not
fully run, or naming something a judge could not have done: `git status`/`-z`/
`rev-parse` failed (on one side or both — the `.git`-removed banner needs the
repo to have actually disappeared, probed independently of `git status`), or
HEAD moved during the review. Where an OS sandbox is actually denying judge
writes, a moved HEAD is a concurrent actor: it is a caution, and so are the
porcelain lines that commit removed — but only for the paths the commit itself
touched, and anything it cannot explain (a new file, a changed content hash)
still hard-stops. Where there is **no containment** (Windows, or an already
nested sandbox) a judge that can write can also commit, so a moved HEAD is a
MUTATION there. A caution about a concurrent commit does not mark the round
degraded: the guard worked, the tree moved, and freshness already covers that. A
caution no longer discards the paid review — the verdict is kept, the notice is
printed, and the round records `**Tamper guard:** degraded — <what>` (or
`clean`). That receipt is load-bearing: a close held to the high-consequence bar
(assertive, irreversible, or an unclassified `## Risk`) **blocks** on such a
round with `TAMPER-GUARD-DEGRADED`, with the same two exits as a stale panel
(re-run the panel, or `--stale-panel-ok --reason "..."` recorded in the receipt);
`reversible` closes proceed with an advisory. Tail certification refuses
outright. The single-judge path carries the same receipt in its judge log and in
the findings it writes into task.md; when no panel round carries a close, the
close reads that log and records the degradation as an advisory receipt clause.

The task's own record directory stays exempt from the working-tree diff, scoped
by whether `task.md` itself is tracked: an **untracked** task.md means the whole
directory is the panel's fresh record dir (broad exemption), while a **tracked**
one exempts only newly created (`??`) record files by name (`judge.md`,
`judge-archive.md`, `judge-*.log`, `task-archive.md`, `vetting-ledger.json`) —
a rogue rewriting a committed `judge.md`, or dropping an `evil.py` beside it,
still flags. task.md content stays guarded by its hash regardless.

**Session-pointer isolation (v1.5.41).** A review or probe spawn runs as a child process that would otherwise inherit the foreground session's identity — `CLAUDE_ENV_FILE` plus the `CLAUDE_CODE_*` / `CLAUDE_PID` vars — and could shadow which task is active, surfacing as a spurious `No active task` during a background panel. Every spawn on the review/probe paths (panel and single-judge reviews, `run_subagent`/`stream_subagent`, and the `claude -p` model-availability probe) now scrubs those parent-session vars through one shared scrubber; writable subagents additionally pin an isolated per-invocation `PLAYBOOK_SESSION_ID`, while read-only judges share a fixed non-foreground `judge` identity (harmless — they cannot write a pointer). Guarantee `PB-SESSION-POINTER-ISOLATION`.

**Task records are written in transactions (v1.5.45).** `tasks/atomic.py` makes
each write all-or-nothing; `tasks/filelock.py` makes the whole
read → transform → write indivisible. The gap was measured, not assumed: the
real close shape — read task.md, run the verify contract for minutes, then write
the receipt — lost a concurrent `tasks blocked` in **5 of 5** trials, leaving the
task `in_progress` with no `## Blocked` section at all.

Three rules define the boundary:

* **Writers serialize, readers never do.** Every read-modify-write of task.md /
  judge.md goes through `atomic.rewrite(path, transform)`, which holds a
  per-task advisory lock (`fcntl` → `msvcrt` → a loud unlocked degrade) on a
  persistent sibling `<name>.lock`. `tasks status`, the gate hook and the
  state-echo hook read on every tool call and take no lock — waiting on a
  minutes-long close would stall the session, and `atomic_write` already
  guarantees they never see a torn file.
* **One transform per VERB, not per helper.** A close is receipt *and* status; a
  handoff is section *and* blocked; a resume is status *and* its stamp. Each is
  composed on the bytes just read and written once, so the pair can never
  half-land. The expensive work (verify, judges) happens outside the lock.
* **A commit re-checks what earned it.** `core.compose_close` refuses — leaving
  task.md byte-identical — when a `## Blocked` appeared, the status moved, or
  the newest panel round changed (its header *or* its findings, since task 085)
  while the close was running, and the close also compare-and-swaps the tree
  fingerprint its freshness decision was made on. Routing alone would not have
  been enough: with the lock neutered the first regression still passed, because
  each writer now reads late; what closes the window is the refusal.
  The other transitions check their source the same way (task 085): blocking
  or handing off refuses a task that is already done, and resuming refuses a
  task that is not `blocked` — so a close that commits first is never
  overwritten by a block or resume that was waiting behind it.

Two-file protocols (`stack_judge_round`'s archive-then-judge.md, `tasks compact`'s
archive-then-task.md) keep their own rollback and hold the lock across the whole
sequence. `tasks tag` rewrites `chat_log.md` while the chat-log hook appends to
it, so it rendezvous on the hook's own lock file **and** compare-and-swaps the
content — it refuses rather than drop a prompt. The destructive-command guard
deliberately takes no lock (a hook must never wait on a writer): it reads the
task once for status and risk, then re-reads the session pointer, and every
error keeps the command blocked.

Honest bounds: the lock is ADVISORY, so a process that writes task.md without
the primitive is not stopped (a structural test fails the build when a new
unapproved writer appears in the package); it is per-task-directory, so it says
nothing about a network filesystem or another machine; and the whole-tree
freshness TOCTOU remains detection-only — a task-directory lock cannot serialize
edits to arbitrary source files during a judge call.

## CI

`.github/workflows/verify.yml` runs `scripts/verify` on four lanes on every push and pull request: Linux (py3.10 and py3.12), macOS (py3.10, plus the zsh logger and the shell fixtures under bash 3.2) and Windows/Git Bash (py3.10). All four must be green for a release, and that is **required by the release checklist (W12), not by GitHub**: `main` has no GitHub branch protection (`gh api repos/{owner}/{repo}/branches/main/protection` returns 404, measured 2026-09-25), so nothing on GitHub stops a push that has not passed. Owner decision D1 (b), 2026-09-24: the lanes stay on every push, and nobody waits on them synchronously — push, then check the run at the next gate that needs it (`gh run watch <id>` inside a background Monitor, or `gh run view <id>` on return). Owner decision D2 (b): a release may rerun once for a disclosed flake whose failure capture is saved into the release record. `concurrency: cancel-in-progress` means a superseded push gets no verdict.

Failure evidence on Windows: the lane exports `PLAYBOOK_VERIFY_FULL_LOG`, so `scripts/verify` appends every child command's complete output — unittest at `-v`, and each shell fixture's full transcript, including the run inside unittest — to `verify-windows-full.txt`, uploaded with the console log `verify-windows.txt`. It is the failing run's own output. Until task 096 a second step re-ran the whole suite and both fixtures on every push (10.8-13.5 min, about half of the Windows job) and uploaded that second run instead.

## Tests

`tests/` — stdlib-unittest suites (no external deps), one file per subsystem: invocation contracts for agy/grok, model-availability machinery, config resolution, mind-map sorting, merge ref-integrity, README-drift detection. Run any file directly: `python3 tests/test_<name>.py`.
