# Tasks CLI reference

Everything the `tasks` CLI does. In a playbook-managed project the agent calls it as `.claude/bin/tasks` — you normally never type these yourself; this page is for understanding what the agent is doing (and for maintainers).

## Workflow

**`tasks work <N>`** — activate task N. Writes the per-session state that the hooks read, which arms the edit gate: from this moment the agent may touch code, and every tool call gets the current gate echoed back at it. Activation is deliberately separate from creation — a task that was merely created enforces nothing. If task N has every gate checked but was never closed, `work <N>` re-adopts it instead of refusing.

**`tasks work done`** — deactivate the current task and mark its status done. This is the only sanctioned way to close a task; editing `## Status` by hand leaves the session state and the document disagreeing. The close runs the project's verify contract and, where policy requires a panel, the freshness gate; two recorded escapes exist — `--force --reason "…"` (the blunt whole-policy override: a failing verify, a missing review, or a stale panel alike) and the narrower `--stale-panel-ok --reason "…"` (accepts only a stale panel), both written into the receipt. See [configuration → the freshness gate](configuration.md#the-freshness-gate---stale-panel-ok) and [the verify-contract guard](configuration.md#the-verify-contract-guard-a-change-to-verify-is-made-visible).

**`tasks freehand`** — freehand mode: you drive, the agent executes, and the hooks stop pressuring for gates until the next task is activated. For exploration, quick experiments, and pairing sessions where the task ceremony would get in the way.

**`tasks blocked "<reason>"`** — pause the active task in an honest `blocked` state while it waits on an owner decision or an external dependency. Not a faked checkbox — the reason is recorded, and `tasks work <N>` clears the block (flipping status back to `in_progress`) when work resumes.

**`tasks handoff`** — hand the active task off to a fresh session. It writes the mechanical part of a handoff — the project repo and each configured `code_roots` nested repo's branch/HEAD/dirty-count, the task's checked/unchecked gate counts and the next unchecked gate, the latest verification-receipt line, and a timestamp — into a `## Handoff` section of the task, then blocks the task in the honest `blocked` state with reason `handoff` (reusing `tasks blocked`, never a faked checkbox). It prints instructions for the agent to append the judgment ~20% the tooling can't know (in-flight reasoning, decisions not yet in the file, dead ends ruled out) under the section's `### Agent notes` scaffold before stopping. A fresh session's `tasks bootstrap` surfaces the newest unconsumed handoff prominently and names the resume command; running `tasks work <N>` resumes the task (flipping status back to `in_progress`), which is what consumes the handoff — the `## Handoff` section stays behind as history. A later handoff on the same task does not overwrite it: the prior `## Handoff` block is archived verbatim (its heading demoted to `### Archived handoff`) under a `## Handoff history` section, newest-first, so no earlier handoff's manually-appended `### Agent notes` are ever lost. Stdlib-only, no daemon, no network; it degrades gracefully when git or the receipts are absent (a handoff on a task with no receipts still works).

**`tasks parked`** — list the out-of-scope findings parked during earlier tasks (`## Parked` sections), so a finding noticed mid-task isn't lost when it's deliberately deferred. Prints "No open parked items." when there are none.

**`tasks audit`** — run the pre-panel sweeps against the active task and record a receipt into its `task.md`; exits non-zero on error-severity findings so it can gate a panel review. Part of the close machinery, not the daily loop. Four built-in mind-map checks run here: `mindmap-stale-refs` (a cited path that no longer exists), `mindmap-node-freshness` (a node whose cited code has changed in ≥2 commits *since the node was last edited* — stale institutional memory; git-only, advisory, tunable via `audit.node_freshness_commits` / `node_freshness_severity`, disable with `audit.node_freshness: false`), `mindmap-dangling-links` (a `[N]` cross-link pointing at a node id that isn't defined anywhere — a dead end the agent follows; fence-aware, names the source node, advisory, `audit.dangling_links_severity` raises it), and `mindmap-wellformed` (structural defects in how the map is written — duplicate node ids, nodes missing a `**bold title**`, and unreachable islands a non-routing node nothing links to; advisory, `audit.wellformed_severity` raises it). A `verify-contract-change` sweep also runs, flagging any `verify` command recorded at a past close but no longer in the current contract for that risk — see [the verify-contract guard](configuration.md#the-verify-contract-guard-a-change-to-verify-is-made-visible).

## Create

**`tasks new <type> <name> [intent]`** — create `.agent/tasks/<N>-<type>-<name>/task.md` from the base template, with the intent text filled in if given. The type selects the workflow pattern the plan will follow: `feature`/`build`/`refactor`/`ops`→Build, `bugfix`/`cleanup`→Fix, `research`→Investigate, `audit`/`eval`→Evaluate. Two standalone small shapes exist besides the patterns: **`quick`** (3 gates, no review machinery — declared-reversible trivia only) and **`light`** (~6 gates: risk classified FIRST with a written why, review routed by risk — the shape for a small change that touches docs, claims, data, or publishing; ceremony is compressed but an `assertive`/`irreversible` classification still requires implementation-review evidence at close, exactly like any other shape). `panel_required_for: ["assertive", "irreversible"]` is the seeded default, so `light`/`quick` reversible work closes on verify+single-judge evidence while claims/data/publish still gate on a panel (set `"all"` for max strictness) — panel evidence is structural (parsed from judge.md rounds), so a checked box can never stand in for a review that didn't run. Recent chat messages are captured into the task's References so the plan can be checked against what you actually said. Creation does **not** activate — the agent should immediately run `tasks work <N>`. On a fresh clone of a multi-user repo (per-user lanes present but the gitignored `.agent/current_user` missing) this refuses rather than create a phantom root lane — set the marker first.

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

**`tasks models detect [--json]`** — fast inventory of the installed agent CLIs (claude / codex / agy / grok / pi) and each one's selectable models plus supported reasoning-effort levels (codex/grok). Reads local caches and the cheap listing commands only — no model is live-probed. (Not strictly offline: `grok models` is login-aware, a server call — but each listing is time-bounded.) This is what `/playbook:init` reads to offer a panel menu; `--json` emits the machine-readable form.

**`tasks models select`** — guided (interactive) refresh of the panel: shows the availability report, takes the new seat list, writes `.agent/models.json` (creating it on fresh installs, preserving keys it doesn't manage).

**`tasks models set --panel a,b --default-judge c [--force]`** — the non-interactive twin of `select`: same spec validation and no-probe availability audit, driven by flags. A dead pin aborts (exit 1) unless `--force` is passed; `--panel` alone leaves the default judge untouched (and vice-versa); `--panel ""` clears the panel. `/playbook:init` uses it to persist the user's chosen panel.

## Analysis & retro

**`tasks retro [--since N]`** — project retrospective across completed tasks: what got built, what patterns recur, where the workflow fought you. Input for pruning the mind map and improving future plans.


**`tasks intent <N>`** — vertical retro of one finished task: several blind extractions infer the task's intent from its different layers (chat, plan, code, tests), the disagreements get reconciled with you, and the distilled result is written to `INTENT.md`. Surfaces the gap between what you asked for and what the trace says happened.

**`tasks context <N>`** — extract the chat messages attributed to task N. Useful when revisiting an old task and the task.md alone doesn't explain a decision.

**`tasks log [N] [--width W]`** — compact one-line-per-message view of the chat log (`.agent/chat_log.md`) — the quick way to scan what was said without the gate echo noise.

**`tasks timeline` / `tasks tagger` / `tasks tag`** — internal retro-support tooling: chronological reconstruction of tasks + messages, and tagging tasks for retro analysis. Not part of the daily workflow.

## Health & merge

**`tasks doctor [--verbose]`** — harness health check: project structure, config shape, judge pins, hook wiring, session state, per-lane gate-logging health, encoding, and (advisory) the environment recommendations below. Advisory findings warn but never fail — doctor's contract is to inform, not block. In the plugin's own source checkout it additionally warns when shipped features have moved past the last README audit (silent everywhere else). It scans every install copy it can find, not just the live one — but a copy belonging to a *different* version (an abandoned marketplace cache from an older release) collapses to one line naming the copy, its version and the finding count, because a stale cache failing checks that shipped after it is expected and used to bury the real findings. `--verbose` enumerates those in full; a foreign copy at the same version as the running code is always enumerated, since it may be a live second install with a real defect.

**`tasks environment [--json] [--suggest-only]`** — advisory report of the optional tools that make playbook run *optimally*, and how to install the ones you're missing: extra vendor agent CLIs (`codex`/`agy`/`grok`/`pi`) so the panel can span vendors, faster-than-grep search/navigation tools (`rg`, `ast-grep`/`sg`, `fd`), the sandbox containment primitive (Linux `bubblewrap` / macOS seatbelt), any verify-command tool not on PATH (a missing one fails close), and the shell-command-logging wiring. Never fails — it suggests, it doesn't install. `/playbook:init` and `tasks doctor` both surface it; `--suggest-only` hides what's already present.

**`tasks detect-verify [--json]`** — deterministic suggestion of a project's full verify command (typecheck **and** tests **and** lint), assembled from the toolchains actually present (Python `pytest`/`mypy`/`pyright`/`ruff`/`flake8`, Node `package.json` scripts, Rust `cargo`, Go `go test`+`vet`, a `Makefile` `test`/`check`/`lint` target). A heuristic starting point that `/playbook:init` shows for you to confirm/correct before writing it to `.agent/config.json` — it never executes anything, and prints a note (no command) when it detects nothing.

**`tasks merge-doctor`** — audit a multi-user repo before/after a merge for the three things plain `git merge` gets wrong in playbook repos: stranded conflict markers in prose files, per-user namespace cross-contamination, and legacy `.agent/` paths.

**`tasks prepare-merge <source> [target]`** — merge preparation used by the merge skill: stages the cross-namespace merge so the verifier can prove it clean.

**`tasks mindmap-sync`** — mind-map merge support (conflict-marker-safe synchronization), also driven by the merge skill.

## Orientation

**`tasks recall <id | keyword…>`** — the fetch half of the bootstrap index, across both mind-map tiers. `tasks recall 12` prints node [12] from `MIND_MAP.md` **and** from `MIND_MAP_OVERFLOW.md` (the deep-detail tier a summarized `↗` node points into) — so pulling a node's full content is one command, not "remember the overflow file exists and grep it by hand." `tasks recall auth policy` is a **ranked relevance search** (BM25 + plural stemming, best node first) across both files — better than grepping one file for an exact word, because a node matching more of the terms (and rarer ones) ranks higher without excluding partial matches. A node can also declare `<!-- keywords: login, credentials -->` so a search finds it by meaning, not just its wording. A topic resolves to node ids you then `recall <N>` in full. This is the "load exactly the node you need, nothing else" retrieval the index was built to feed. Pure stdlib and offline — no embeddings or vector index, deliberately, to keep the plugin portable.

**`tasks bootstrap`** — session-start orientation: prints the mind map (the project's memory), pending tasks, and the CLI reference. The agent runs this as its first action in every session — it's how session thirty picks up from session one. A small mind map prints in full; once it grows past the bootstrap budget (~8 KB) it prints as an **index** — routing nodes [1]-[5] in full plus a one-line titled TOC of every other node — so orientation loads the map's shape, not thousands of tokens of subsystem prose the task never reads. The agent greps the two or three nodes its task touches (`grep '^\[18\]' MIND_MAP.md`). The judge/review path keeps the fuller whole-node trim, since auditing needs whole nodes.

**`tasks list [--pending]`** (alias `ls`) — task overview table; `--pending` hides finished work.

**`tasks status`** — the active task's current gate position: the fastest way to see where a long run actually is.

**`tasks compact <N> [--dry-run]`** — the mechanical half of the sanctioned task.md compaction. An open task.md grows monotonically (every review round, every outcome note) until a judge reads it through a trimmed keyhole (`audit`'s `task-bloat` check flags it). Wrap each cold block — old review-round narrative, never gates or Intent/Design/Parked — in `<!-- archive:start -->` … `<!-- archive:end -->`, then run this: it appends every marked block **verbatim** to `task-archive.md` (same dir) and leaves a one-line pointer. The agent decides what's cold; the command guarantees the move is safe — an unmatched marker, or a block containing a gate checkbox, a `<!-- pin -->`, or a protected section heading, aborts the whole run and writes nothing. `--dry-run` previews. "Moving history is not deleting it."

**`tasks init [--provider codex|antigravity|grok|pi] [--hooks]`** — creates the `.agent/` tasks structure, `CLAUDE.md`, and the `MIND_MAP.md` stub. With `--provider`, additionally writes that agent's bootstrap file (`AGENTS.md` / `GEMINI.md`) and, with `--hooks`, installs its hook integration — see [providers](providers.md). The FULL mechanical scaffolding — `.claude/bin/` wrappers, `.claude/settings.json` hook registrations, `.agent/config.json` (including the seeded risk-gated close bar `panel_required_for: ["assertive", "irreversible"]` — reversible work closes without a panel, only claims/data/publish require one; `"all"` is available for max strictness), and the `.gitignore` runtime-state block — is done by `scripts/init`, run via the `/playbook:init` slash command (the normal entry point). Running the bare `tasks init` CLI alone does not seed those. On a fresh clone of a multi-user repo (per-user lanes present but the gitignored `.agent/current_user` missing) this refuses rather than create a phantom root lane — set the marker first.

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

Six skill bundles are discovered by the agent harness's plugin skill mechanism (each carries a `SKILL.md`: playbook, judge, monitor, merge, stack, testing). A further directory, `skills/tasks/`, is not a harness-discoverable skill — it holds the canonical task template the `new` command copies. (None are printed by `tasks bootstrap`, which prints the mind map, pending tasks, and CLI reference.)

| Skill | What it does |
|---|---|
| **playbook** | The composable workflow patterns (Build, Fix, Investigate, Evaluate, UI-debug, reflection gates) that task plans are built from. |
| **judge** | The blind-evaluation pattern: spawn an independent judge with repo access but no conversation, verdict to a shared file. |
| **monitor** | The trajectory watcher: a second agent reads the session transcript incrementally and nudges when the work drifts ([architecture](architecture.md)). |
| **merge** | End-to-end verified branch merge for multi-user playbook repos — namespace contamination checks, mind-map conflict handling, deterministic verifiers, optional `--push`. |
| **stack** | Default tech-stack picks ("Bedrock stack") for scaffolding fresh projects when nothing is specified — boring, typed, observable. |
| **tasks** | The canonical task template the `new` command copies. |
