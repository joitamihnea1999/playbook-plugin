# Architecture

How the plugin is put together, and how the enforcement actually works.

## Layout

The plugin (`plugins/playbook/` in this repo) has four user-visible parts plus two engine directories:

- `commands/` — seven `/playbook:*` slash commands (markdown the agent executes as instructions).
- `skills/` — five harness-discoverable skill bundles, each with a `SKILL.md` (playbook patterns, judge, monitor, merge, stack), plus `skills/tasks/` which holds the canonical task template the `new` command copies (not a discoverable skill).
- `hooks/hooks.json` — six lifecycle hook registrations (below).
- `scripts/` — executable entry points: the `tasks` dispatcher, hook scripts, `sandbox`, `monitor`, `init`, the `playbook-*` provider launchers.
- `tasks/` — the Python package behind the `tasks` CLI (dispatcher sets `PYTHONPATH` here).
- `provider/` — provider adapters and judge-dispatch machinery ([providers](providers.md)).

Everything is plain files — bash entry points, Python 3 stdlib, markdown as the runtime language. No build step, no dependencies.

## Hooks & enforcement

Six hooks enforce the structure at the OS level, because warnings don't stick — blocking does:

| Hook | What it does |
|---|---|
| `SessionStart` | Runs bootstrap orientation (mind map + pending tasks + CLI reference). |
| `PreToolUse` (matcher `Edit\|Write\|search_replace\|write\|Bash\|Shell\|StrReplace\|run_terminal_command`) | The **task gate**: BLOCKS code edits when no task is active. Grok names (`write`, `search_replace`, `run_terminal_command`, `Shell`, `StrReplace`) map to Claude Edit/Write/Bash via the normalizer — same gate, every provider. |
| `UserPromptSubmit` | Appends every user message to `.agent/chat_log.md` (timestamped, agent-tagged — feeds task attribution and `tasks log`). |
| `PostToolUse` | Echoes gate state after every tool call, keeping the current gate in the agent's face. |
| `Stop` / `SessionEnd` | Finalize session state. SessionEnd removes the session directory only when the process is really exiting — `/clear` keeps it, because the same process continues and the active task has to survive it. |

`/playbook:init` additionally writes a **deny-list** into the project's `.claude/settings.json` blocking `TodoWrite`, `Task`, and `EnterPlanMode` — those would compete with task.md as the source of truth. If those tools suddenly error in a playbook project, that's why.

A `bash-log` shell integration records commands into `.agent/bash_history`, so terminal work is auditable alongside the chat log.

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

`.claude/bin/sandbox` runs the agent with `--dangerously-skip-permissions` inside OS-level containment: **macOS seatbelt** or **Linux bubblewrap** with deny-write-by-default. The project directory is writable, `.git` is read-only (history can't be mangled), everything outside is blocked at the kernel level. One honest caveat: when no containment primitive is available (neither `sandbox-exec` nor `bwrap`, or nesting inside a foreign sandbox forbids it), the agent currently runs with bypass flags and **no** kernel containment — check your platform has one of the two before relying on the blast-radius guarantee. Pairs with the task system for the "two agents, one task" pattern: orchestrator outside, worker inside, task.md as the handoff.

## Judges

Blind by construction: a judge gets the repo but not your conversation, so it can't anchor to whatever was already agreed in chat. Single judge (`plan-review` / `impl-review`) writes findings into the task.md; the panel (`panel-review`) fans out to every seat in `models.json` in parallel and writes `judge.md`. Judge output is triaged, not obeyed — the task template's review gates require an accept/park/reject decision per finding.

Since v1.4.3 the judge process is also read-only: sandboxed with the project mounted no-write, so a judge physically cannot edit the repo or task.md. Because that OS containment is unavailable on some platforms (Windows, nested sandboxes), a working-tree tamper guard backs it up — the review paths snapshot git status + the task.md hash before and after, and if a judge changed anything the review is saved with a loud TAMPER banner, ingestion is refused, and the run exits non-zero.

## Tests

`tests/` — stdlib-unittest suites (no external deps), one file per subsystem: invocation contracts for agy/grok, model-availability machinery, config resolution, mind-map sorting, merge ref-integrity, README-drift detection. Run any file directly: `python3 tests/test_<name>.py`.
