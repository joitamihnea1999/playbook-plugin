# Tasks CLI reference

Everything the `tasks` CLI does. In a playbook-managed project the agent calls it as `.claude/bin/tasks` — you normally never type these yourself; this page is for understanding what the agent is doing (and for maintainers).

## Workflow

**`tasks work <N>`** — activate task N. Writes the per-session state that the hooks read, which arms the edit gate: from this moment the agent may touch code, and every tool call gets the current gate echoed back at it. Activation is deliberately separate from creation — a task that was merely created enforces nothing. If task N has every gate checked but was never closed, `work <N>` re-adopts it instead of refusing.

**`tasks work done`** — deactivate the current task and mark its status done. This is the only sanctioned way to close a task; editing `## Status` by hand leaves the session state and the document disagreeing.

**`tasks freehand`** — freehand mode: you drive, the agent executes, and the hooks stop pressuring for gates until the next task is activated. For exploration, quick experiments, and pairing sessions where the task ceremony would get in the way.

## Create

**`tasks new <type> <name> [intent]`** — create `.agent/tasks/<N>-<type>-<name>/task.md` from the base template, with the intent text filled in if given. The type selects the workflow pattern the plan will follow: `feature`/`build`/`refactor`/`ops`→Build, `bugfix`/`cleanup`→Fix, `research`→Investigate, `audit`/`eval`→Evaluate. Two standalone small shapes exist besides the patterns: **`quick`** (3 gates, no review machinery — declared-reversible trivia only) and **`light`** (~6 gates: risk classified FIRST with a written why, review routed by risk — the shape for a small change that touches docs, claims, data, or publishing; ceremony is compressed but an `assertive`/`irreversible` classification still requires implementation-review evidence at close, exactly like any other shape). Projects adopting `light` should set `panel_required_for: ["assertive", "irreversible"]` (or `"all"`) in `.agent/config.json` — panel evidence is structural (parsed from judge.md rounds), so a checked box can never stand in for a review that didn't run. Recent chat messages are captured into the task's References so the plan can be checked against what you actually said. Creation does **not** activate — the agent should immediately run `tasks work <N>`. On a fresh clone of a multi-user repo (per-user lanes present but the gitignored `.agent/current_user` missing) this refuses rather than create a phantom root lane — set the marker first.

**`tasks new --stub <type> <name> [intent]`** — create a lightweight stub that expands into the full template when activated. For capturing future work the moment you think of it, without paying the template cost yet.

## Review (judges)

The review commands exist because an agent that reviews its own plan inside the conversation just agrees with itself. A judge is a separate headless agent that sees the repository but **not** your chat — so it can't anchor to the approach already committed to.

A plan review runs six lenses — intent alignment, failure modes, hostile sequences, test coverage, simplify, and prove-it (file:line evidence, not assertions). An implementation review runs six of its own — simplify, self-critique, bug scan, hostile sequences, test quality, prove-it-works.

The **hostile-sequence** lens walks every state-changing flow the change touches: two concurrent requests, the same logical event delivered twice under distinct ids, reordered events, an external call succeeding while the local transaction rolls back, a crash after commit but before any post-commit step, and a retry after a lost response. For each one it asks for the invariant the design relies on and the test that proves it. A change touching no shared or persisted state says so in one line, so the lens stays sharp instead of manufacturing findings.

**`tasks plan-review <N>`** — single blind judge reads task N's plan before any code is written; findings are inserted into the task.md, where the agent must triage each one (accept/park/reject) rather than obey blindly.

**`tasks impl-review <N>`** — the same, after implementation: does every Intent claim trace down through code to tests?

**`tasks panel-review [<N>]`** — fan the review out to every judge seat in `.agent/models.json` in parallel (different models, different providers — panels catch problems a single judge misses); results land in the task's `judge.md`. The task number is optional: `--prompt "..."` alone turns it into a general-purpose multi-model consultation on any question, and `--bare` strips the repo context too. Other flags: `--mode impl` (implementation stage), `--models a,b` (override the panel for one run), `--timeout <secs>` / `--budget <usd>` (override [configuration](configuration.md) for one run).

**`tasks judge`** — the low-level runner behind the review commands; use `plan-review` / `impl-review` instead.

**`tasks models check [--no-probe]`** — audit every judge pin against live availability. Pinned model ids rot as providers retire models; this catches it before a review silently degrades. Probes cost a few tiny model calls; `--no-probe` is the free degraded audit. Exits 1 when a pin can't run as configured.

**`tasks models detect [--json]`** — fast, no-network inventory of the installed agent CLIs (claude / codex / agy / grok / pi) and each one's selectable models plus supported reasoning-effort levels (codex/grok). Reads local caches and cheap listing commands only — nothing is live-probed. This is what `/playbook:init` reads to offer a panel menu; `--json` emits the machine-readable form.

**`tasks models select`** — guided (interactive) refresh of the panel: shows the availability report, takes the new seat list, writes `.agent/models.json` (creating it on fresh installs, preserving keys it doesn't manage).

**`tasks models set --panel a,b --default-judge c [--force]`** — the non-interactive twin of `select`: same spec validation and no-probe availability audit, driven by flags. A dead pin aborts (exit 1) unless `--force` is passed; `--panel` alone leaves the default judge untouched (and vice-versa); `--panel ""` clears the panel. `/playbook:init` uses it to persist the user's chosen panel.

## Analysis & retro

**`tasks retro [--since N]`** — project retrospective across completed tasks: what got built, what patterns recur, where the workflow fought you. Input for pruning the mind map and improving future plans.


**`tasks intent <N>`** — vertical retro of one finished task: several blind extractions infer the task's intent from its different layers (chat, plan, code, tests), the disagreements get reconciled with you, and the distilled result is written to `INTENT.md`. Surfaces the gap between what you asked for and what the trace says happened.

**`tasks context <N>`** — extract the chat messages attributed to task N. Useful when revisiting an old task and the task.md alone doesn't explain a decision.

**`tasks log [N] [--width W]`** — compact one-line-per-message view of the chat log (`.agent/chat_log.md`) — the quick way to scan what was said without the gate echo noise.

**`tasks timeline` / `tasks tagger` / `tasks tag`** — internal retro-support tooling: chronological reconstruction of tasks + messages, and tagging tasks for retro analysis. Not part of the daily workflow.

## Health & merge

**`tasks doctor`** — harness health check: project structure, config shape, judge pins, hook wiring, session state, per-lane gate-logging health, encoding, and (advisory) the environment recommendations below. Advisory findings warn but never fail — doctor's contract is to inform, not block. In the plugin's own source checkout it additionally warns when shipped features have moved past the last README audit (silent everywhere else).

**`tasks environment [--json] [--suggest-only]`** — advisory report of the optional tools that make playbook run *optimally*, and how to install the ones you're missing: extra vendor agent CLIs (`codex`/`agy`/`grok`/`pi`) so the panel can span vendors, the sandbox containment primitive (Linux `bubblewrap` / macOS seatbelt), any verify-command tool not on PATH (a missing one fails close), and the shell-command-logging wiring. Never fails — it suggests, it doesn't install. `/playbook:init` and `tasks doctor` both surface it; `--suggest-only` hides what's already present.

**`tasks detect-verify [--json]`** — deterministic suggestion of a project's full verify command (typecheck **and** tests **and** lint), assembled from the toolchains actually present (Python `pytest`/`mypy`/`pyright`/`ruff`/`flake8`, Node `package.json` scripts, Rust `cargo`, Go `go test`+`vet`, a `Makefile` `test`/`check`/`lint` target). A heuristic starting point that `/playbook:init` shows for you to confirm/correct before writing it to `.agent/config.json` — it never executes anything, and prints a note (no command) when it detects nothing.

**`tasks merge-doctor`** — audit a multi-user repo before/after a merge for the three things plain `git merge` gets wrong in playbook repos: stranded conflict markers in prose files, per-user namespace cross-contamination, and legacy `.agent/` paths.

**`tasks prepare-merge <source> [target]`** — merge preparation used by the merge skill: stages the cross-namespace merge so the verifier can prove it clean.

**`tasks mindmap-sync`** — mind-map merge support (conflict-marker-safe synchronization), also driven by the merge skill.

## Orientation

**`tasks bootstrap`** — session-start orientation: prints the mind map (the project's memory), pending tasks, and the CLI reference. The agent runs this as its first action in every session — it's how session thirty picks up from session one.

**`tasks list [--pending]`** (alias `ls`) — task overview table; `--pending` hides finished work.

**`tasks status`** — the active task's current gate position: the fastest way to see where a long run actually is.

**`tasks init [--provider codex|antigravity|grok|pi] [--hooks]`** — creates the `.agent/` tasks structure, `CLAUDE.md`, and the `MIND_MAP.md` stub. With `--provider`, additionally writes that agent's bootstrap file (`AGENTS.md` / `GEMINI.md`) and, with `--hooks`, installs its hook integration — see [providers](providers.md). The FULL mechanical scaffolding — `.claude/bin/` wrappers, `.claude/settings.json` hook registrations, `.agent/config.json` (including the seeded `panel_required_for: "all"` close bar), and the `.gitignore` runtime-state block — is done by `scripts/init`, run via the `/playbook:init` slash command (the normal entry point). Running the bare `tasks init` CLI alone does not seed those. On a fresh clone of a multi-user repo (per-user lanes present but the gitignored `.agent/current_user` missing) this refuses rather than create a phantom root lane — set the marker first.

## Slash commands (user-invoked)

| Command | What it does |
|---|---|
| `/playbook:init` | Initialize or upgrade a project for the playbook workflow (runs `tasks init` + scaffolding). |
| `/playbook:mindmap` | Generate `MIND_MAP.md` by analyzing the codebase — the agent's persistent memory; run this right after init. |
| `/playbook:mindmap-optimize` | Audit the mind map for staleness, compression opportunities, and sync issues. |
| `/playbook:playbook` | Show the workflow patterns reference (Build/Fix/Investigate/Evaluate) — how the agent structures plans. |
| `/playbook:freehand` | Enter freehand mode — user drives, no gate pressure (same as `tasks freehand`). |
| `/playbook:intent` | Vertical retro of a finished task, distilled to `INTENT.md` (front-end to `tasks intent`). |
| `/playbook:upgrade` | Upgrade the plugin to the latest version. |

## Skills (agent-loaded)

Five skill bundles are discovered by the agent harness's plugin skill mechanism (each carries a `SKILL.md`: playbook, judge, monitor, merge, stack). A sixth directory, `skills/tasks/`, is not a harness-discoverable skill — it holds the canonical task template the `new` command copies. (None are printed by `tasks bootstrap`, which prints the mind map, pending tasks, and CLI reference.)

| Skill | What it does |
|---|---|
| **playbook** | The composable workflow patterns (Build, Fix, Investigate, Evaluate, UI-debug, reflection gates) that task plans are built from. |
| **judge** | The blind-evaluation pattern: spawn an independent judge with repo access but no conversation, verdict to a shared file. |
| **monitor** | The trajectory watcher: a second agent reads the session transcript incrementally and nudges when the work drifts ([architecture](architecture.md)). |
| **merge** | End-to-end verified branch merge for multi-user playbook repos — namespace contamination checks, mind-map conflict handling, deterministic verifiers, optional `--push`. |
| **stack** | Default tech-stack picks ("Bedrock stack") for scaffolding fresh projects when nothing is specified — boring, typed, observable. |
| **tasks** | The canonical task template the `new` command copies. |
