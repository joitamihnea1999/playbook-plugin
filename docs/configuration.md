# Configuration

Two JSON files, both under `.agent/` in your project and hand-editable: `config.json` is created by `/playbook:init` (which also seeds its `verify` command after confirming it with you); `models.json` is created by `/playbook:init` when you pick a panel other than the shipped all-Claude default — or any time via `tasks models select` / `tasks models set` (or by hand). `models.json` holds machine-specific judge pins and is gitignored by design.

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

## `.agent/config.json` — judge & close knobs

Three more `.agent/config.json` keys tune the judge panel and the close-time verify run:

```json
{
  "panel_quorum": "majority",
  "judge_verify": ["python3 -m pytest -q"],
  "verify_timeout_secs": 1200,
  "review_context_chars": 100000,
  "review_context_chars_stdin": 200000
}
```

- `panel_quorum` — the minimum number of succeeding judges for a `panel-review` to **PASS**. Accepted values: `"majority"` (the default — `launched // 2 + 1`, a strict majority of the judges that actually *launched*, so a degraded panel with seats missing still needs a real majority rather than quietly lowering the bar), `"all"` (every launched judge must pass), a positive integer (an absolute count), or a float in `(0, 1]` (that fraction of the launched judges, rounded up). A bool, a value `≤ 0`, or an out-of-range fraction is **invalid**; an invalid value at any tier warns once and **falls through to the next tier** — env → config → the built-in `"majority"` — so a bad env var does not mask a valid `config.json` value, and `"majority"` applies only when no tier is valid. **Precedence, highest first:** `PLAYBOOK_PANEL_QUORUM` env → `.agent/config.json` `panel_quorum` → the built-in `"majority"`.
- `judge_verify` — a list of shell command strings the project declares safe for a judge to run inside its **read-only sandbox** while checking a specific suspicion. **This is prompt guidance, not an enforced execution engine, and whether a judge can actually run the commands depends on its seat's tools:** Claude judge seats are invoked with `--tools Read,Glob,Grep` (plus `WebSearch`) and **cannot execute shell commands at all** — for them the clause is advisory context; codex/grok seats decide for themselves. Only the **first six** declared commands are surfaced to the judge prompt (declare the ones that matter first). Declare only commands that never write inside the repo — redirect caches elsewhere, use a unique temp dir, keep them parallel-safe — because judges run concurrently under the sandbox. Absent or empty (the default) means no execution clause is added at all. A non-list value, or non-string / blank entries, are ignored.
- `verify_timeout_secs` — the **hard** wall-clock ceiling, in seconds, for **one** declared `verify` command at close (`tasks work done`): on expiry that command is killed and the close is blocked. Default 1200. Set it to `0` or `"unlimited"` (also the JSON strings `"none"` / `"null"` / `"inf"` / `"infinite"`) for **no ceiling** — this knob exists because a verify command with no ceiling can hang `tasks work done` forever, which in headless use is a silent deadlock. **Precedence, highest first:** `PLAYBOOK_VERIFY_TIMEOUT_SECS` env → `.agent/config.json` `verify_timeout_secs` → the 1200 default. This is distinct from `review_timeout_secs`, which bounds the *judge* subprocess, not the verify command.
- `review_context_chars` / `review_context_chars_stdin` — the per-transport character budget for the task context handed to a review judge. Two keys because the two transports differ: `review_context_chars_stdin` (default **200000**) applies to stdin-fed seats (claude, codex), which have no OS argv limit so their ceiling is model attention; `review_context_chars` (default **100000**) applies to argv-fed seats (grok and the experimental agy/pi), which stay under the byte-guarded argv bound. Raising a budget past what the transport can carry is reported in the review receipts. **Precedence, highest first, per key:** `PLAYBOOK_REVIEW_CONTEXT_CHARS` / `PLAYBOOK_REVIEW_CONTEXT_CHARS_STDIN` env → `.agent/config.json` → the default.

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

## `.agent/config.json` — `standing_gates` (project policy)

Gates your project wants on **every** task — a journal entry, a changelog line — declared once instead of hand-added (and hand-relocated) per task:

```json
{
  "standing_gates": [
    {"title": "Journal", "text": "Write journal/{{NNN}}.md — Shipped / Friction / Value / Honesty-check / One-change"},
    {"title": "Changelog", "text": "Add the user-visible change to CHANGELOG.md"}
  ]
}
```

Each entry becomes a `## <title>` section with a single `- [ ] <text>` gate, appended in declared order as the **final gates** of every generated task — base templates, `quick`, custom `.agent/playbooks/` templates, and stub expansion alike (a stub gains them when it expands at activation). `{{NNN}}` in title or text substitutes the zero-padded task number. The Stop hook enforces them like any other gate.

Rules, all loud: the key is **opt-in** (absent means task generation is byte-identical to before — `init` seeds none); title and text are collapsed to a single line so a config value can never mint a phantom section or gate; an entry that is malformed, or whose title collides with a section the task already has, is skipped with a printed warning — never silently written, never silently dropped. Like `merge_verify`, this is project policy: commit the file so every clone generates the same tasks.

## `.agent/config.json` — `fingerprint_exclude` (project policy)

Panels stamp the tree-state fingerprint they reviewed; close compares it and
records `FRESH`/`STALE` in the receipt, and — **when policy requires a panel
for that close** (`panel_required_for`) — a close held to the high-consequence
bar (**assertive, irreversible, or an unset/`unclassified` `## Risk`**) resting
on a stale panel BLOCKS (see below). Under the seeded default
`panel_required_for: ["assertive","irreversible"]` that covers assertive and
irreversible; `unclassified` is caught only where policy also requires a panel
for it (e.g. `"all"`). The fingerprint already ignores `.agent/`
— workflow bookkeeping is not code. If your project has **owner-declared
bookkeeping outside `.agent/`** that standing gates write after the last panel
(the canonical case: a `journal/` directory), declare it:

```json
{
  "fingerprint_exclude": ["journal/"]
}
```

Entries are git pathspec strings, appended to the exclusion set for both the
panel stamp and the close comparison. Malformed entries are skipped with a
printed warning, never silently. Exclude only true bookkeeping: anything
excluded here can change after a panel without anyone being told, so a path
that can carry claims or code does NOT belong in this list. Commit the file —
stamp and close must agree across clones.

## `.agent/config.json` — `code_roots` (project policy)

The tree-state fingerprint is computed from the OUTER git repository. Some
projects keep their real code in a **gitignored nested checkout** — this
workspace's `playbook-plugin/`, HowFar-v2's app repo — invisible to the outer
`git status`. A code-only edit inside such a nested repo therefore does not move
the outer fingerprint at all, so a post-panel change there reads silently
**FRESH** and the freshness gate never fires — the unsafe direction of the
guarantee. `code_roots` closes that blind spot:

```json
{
  "code_roots": ["playbook-plugin"]
}
```

Each entry is a **project-relative path** to a nested git repository. When set,
the fingerprint additionally folds in each root's `HEAD`, porcelain status, and
working diff (including untracked-file content) — the exact same material the
outer tree already hashes, no stronger and no weaker. A code-only edit inside a
listed root now moves the fingerprint, so the freshness gate sees it.

Rules, all loud: the key is **opt-in** — with `code_roots` absent (or `[]`) the
fingerprint is **byte-identical** to before the key existed, so nothing changes
for projects that don't use it. Entries are sorted and de-duplicated so config
order can never perturb the hash. An entry that is not a string, is empty, is an
absolute path, or contains `..` traversal is skipped with a printed warning; a
symlinked entry that *resolves* outside the project is likewise skipped loudly
(the fingerprint is never steered to hash something outside the tree). Each root
must be its **own** git repository — a plain subdirectory of the outer repo, a
path that does not exist yet, or a repo git cannot read contributes a stable
`<absent>` marker rather than adopting the ancestor repo's identity or crashing
the fingerprint. Commit the file — the panel stamp and the close comparison must
agree across clones.

The same exclusion set applies inside every nested root as in the outer tree:
`.agent/` is always excluded (workflow bookkeeping is not code), and any
[`fingerprint_exclude`](#agentconfigjson--fingerprint_exclude-project-policy)
pathspecs you declare are honored in each nested root too. So a
`fingerprint_exclude: ["journal/"]` blinds a `journal/` directory inside a
`code_roots` repo as well as in the outer one — deliberate (the exclusion is a
project-wide bookkeeping declaration), but worth knowing before you exclude a
path that also exists under a nested root.

## `.agent/config.json` — `audit` (pre-panel sweeps)

`tasks audit` runs mechanical sweeps before a review so judges spend tokens on hard problems, not greppable ones. The `audit` key tunes them:

```json
{
  "audit": {
    "sweeps": [
      {"name": "no-print", "command": "! grep -rn 'console.log' src", "why": "stray debug logs", "severity": "advisory"}
    ],
    "disable_defaults": false,
    "mindmap_severity": "advisory",
    "node_freshness": true,
    "node_freshness_severity": "advisory",
    "dangling_links_severity": "advisory",
    "wellformed_severity": "advisory",
    "task_bloat_chars": 24000
  }
}
```

- `sweeps` — project-specific sweeps appended to the built-in safety set. Each is `{name, command, why, severity}`; a sweep's shell `command` exits 0 = findings, 1 = clean, ≥2 = error/did-not-run (never a pass). Malformed entries (missing `name`/`command`) are skipped rather than crashing the audit. `severity` is one of `error` / `advisory` / `info` (default `advisory`); only `error` findings fail the audit.
- `disable_defaults` — set `true` to drop the built-in default sweeps (conflict-markers, merge-artifacts, stale-markers) and run only your declared `sweeps`. Default `false`.
- `node_freshness` — set `false` to turn off the mind-map node-freshness sweep (which flags a node whose cited code changed after the node). Default on.
- `mindmap_severity`, `node_freshness_severity`, `dangling_links_severity`, `wellformed_severity` — per-sweep severity overrides for the advisory mind-map checks; each takes `error` / `advisory` / `info`. Raise one to `error` to make that drift fail the audit instead of merely reporting.
- `task_bloat_chars` — the byte threshold above which an open `task.md` is flagged as too large to review through one window (the sweep nudges the sanctioned `tasks compact`). Unset or `≤ 0` defaults to **half the argv `review_context_chars` budget** (≈50000 at the default), so it tracks your context budget automatically.

## `.agent/config.json` — `command_guard` (destructive-command interlock)

A PreToolUse hook (`command-guard-hook`) blocks unambiguous high-blast /
irreversible shell commands — `rm -rf` on a dangerous path, `git push --force`,
`git reset --hard`, `git clean -fd`, `curl|sh`, `dd`/`mkfs` to a device, a DB
`DROP`/`TRUNCATE` — before they run, because the sandbox contains filesystem
blast radius but not outward/logical irreversibility, and judgment alone is not a
guarantee. It is **conservative** (matches only at a command position, so an
`echo`/`grep` of dangerous text is fine; a relative `rm -rf ./build` is fine;
`--force-with-lease` is allowed), and it **fails OPEN** on any internal error so
it can never wedge a session.

- **Acknowledge** a command you've confirmed: run it with `PLAYBOOK_ALLOW_DANGEROUS=1`,
  or inside a task classified `## Risk: irreversible` with a rollback plan.
- **Extend** with project-specific patterns (e.g. a deploy/publish command):
  ```json
  {"dangerous_commands": ["^fly deploy\\b", "npm publish"]}
  ```
  Entries are case-insensitive regexes matched against the whole command.
- **Disable** entirely: `{"command_guard": false}`.

It's a safety interlock against the agent's *mistake*, not an adversary — for
adversarial containment run the agent in the sandbox (OS-level). Like the rest
of the hook enforcement, it is a **supported** interlock only on Claude (owner
decision 2026-08-21); the grok and codex command-guard paths ship but are
experimental, and antigravity/pi are experimental everywhere. See the
[provider support matrix](providers.md#support-matrix).

### An unset `## Risk` is held to the high-consequence bar

`panel_required_for` keys the review requirement to the risk class, so an
`unclassified` task used to fall through it entirely and close on a warning —
making a blank field the cheapest path through the strictest gate in the system,
chosen by the same agent the gate constrains. Since 1.5.32 the close
distinguishes two facts that were previously conflated:

- **no `## Risk` heading at all** — a pre-1.5.0 task that was never offered the
  gate. Closes, with the warning naming what could not be evaluated. Unchanged.
- **heading present but unset** (or malformed, e.g. the one-line
  `## Risk: assertive`) — the gate was offered and skipped. `tasks work done`
  blocks unless there is impl-review evidence or `--force --reason`, exactly as
  for `assertive` / `irreversible`.

Setting `## Risk` to one word clears it. `## Risk Routing` in the light template
is a gate checklist, not the classification field, and does not trigger this.

### The freshness gate (`--stale-panel-ok`)

The gate fires only when policy requires a panel for the close
(`panel_required_for`). When that holds and the close is held to the
high-consequence bar — `## Risk` is `assertive` or `irreversible`, **or it is
left unset/`unclassified`** (held to that same bar everywhere else) — and the
newest impl round's stamp no longer matches the tree, `tasks work done` blocks:
the panel's verdict predates the code being closed — a claim (assertive), an
unrecoverable act (irreversible), or work whose risk was never classified,
signed off by a panel that predates the code, is a decision about code that was
never reviewed. (Blocking only assertive/irreversible would make blanking
`## Risk` strictly more lenient on freshness than honest classification.) Note
the precondition: under the seeded default `panel_required_for:
["assertive","irreversible"]`, `unclassified` does not require a panel, so its
freshness block engages only where policy also requires a panel for it (e.g.
`"all"`). Two exits, both on the record —

- re-run `tasks panel-review <N> --mode impl` (fresh evidence), or
- `tasks work done --stale-panel-ok --reason "..."` — closes, and the reason
  lands in the receipt's freshness clause (`STALE, accepted: "..."`).

`--stale-panel-ok` suppresses only this gate; verify failures, gate bounces,
and the panel-evidence requirement are untouched. `--force --reason` remains the
blunt whole-policy hatch. A `reversible` risk always stays advisory (console
note + receipt clause, no block), as does any close for which policy does not
require a panel.

The stamp compares a tree-state fingerprint of the **outer** git repository. If
your project keeps code in a gitignored **nested checkout** (this workspace's
`playbook-plugin/`), a code-only edit there is invisible to the outer fingerprint
and would read silently FRESH — declare those repos in
[`code_roots`](#agentconfigjson--code_roots-project-policy) so the freshness gate
sees them.

#### Tail certification (a docs/test tail need not re-run the whole panel)

The late rounds of a long assertive task are usually docs/comment/test-only —
and re-running a full multi-model panel for each such tail burns vendor quota,
so those closes used to be waved through with `--stale-panel-ok` on every round.
Tail certification (owner decision A, 2026-08-27) replaces that rubber stamp
with a real, cheap check. When the panel is STALE and would block, `tasks work
done` computes the exact delta since the panel's tree state (F0), across the
outer tree and every `code_roots` scope. If **every** changed path is in a
**non-behavioral file class** — `*.md` under `docs/` or named
`README*`/`CHANGELOG*`/`MIND_MAP*`, `*.json` under `docs/` (the guarantee
ledger), the repo-root `CLAUDE.md`, anything under `tests/`, and task records
under `.agent/` — the **default single judge** re-reviews just that delta against
the panel's verdict and returns a structured `TAIL-CERT: PASS`/`FAIL`. A PASS
satisfies freshness and the close is recorded as `STALE, but TAIL-CERTIFIED`.

The safety property is that **any behavioral (code) delta still forces a fresh
full panel** — a single changed line in a `.py` (even a comment), a `config.json`
edit, a top-level doc that isn't a recognized doc name, or a rename that moves
code into `docs/` (both endpoints are classified, so the deleted source is seen).
The mechanism fails closed on every ambiguity: a missing/mismatched panel
descriptor, a `code_roots` set that changed since the panel, a git error, a
stale tree with no attributable delta, a non-PASS verdict, or a tree that mutates
during the judge call. `--stale-panel-ok --reason` and `--force --reason` remain
the manual exits; tail certification is the automatic one for the docs/test tail.

### The verify-contract guard (a change to `verify` is made visible)

`verify` lives in `.agent/config.json`, which is on the management path and so
is **exempt from the code-edit gate** — an agent can edit it without an active
task. That is deliberate (task bookkeeping must stay editable), but it means the
verify command itself could be silently weakened or deleted and tasks then
closed against a hollow bar. Two guards make such a change visible, without
hard-blocking a legitimate improvement:

- **Journalled at close.** Every close records the verify commands it ran in the
  enforcement journal (`.agent/**/journal/enforcement.jsonl`, `hook: "close"`,
  `decision: "record"` — the journal `decision` contract is `{allow, block,
  record}`, and a close is a log-only `record`, not a gate allow/block) in
  addition to naming them in the task's
  `## Verification Receipt`. The commands are newline-escaped onto a single line,
  so a multi-line verify command is kept intact (not chopped at its first
  newline, which would silently drop every command after it). The `command`
  field is still capped at pb_journal's 200-**byte** head limit (byte, not
  character — 200 multi-byte chars would blow the `PIPE_BUF` atomic-single-write
  bound) — so a very long command set is truncated there; the task's `## Verification Receipt`
  remains the authoritative, uncapped per-command list. Best-effort and
  log-only: a journal failure never changes or blocks a close.
- **Flagged by `tasks audit`.** The `verify-contract-change` sweep flags any
  command that was run at **some past close** (the union of every close receipt
  across **all lanes** — the contract is repo-global — so a weaken-then-close, a
  fresh lane, or a different user's history cannot hide a weakening) but is no
  longer in the current contract **for the risk that close ran under**. The
  comparison is *per-risk*: a command recorded at a `reversible` close is checked
  against today's `reversible` bar, so moving a command out of `_always` and into
  a single risk key — which quietly drops it from every *other* risk's bar — is
  caught, where an all-risk-union check would have called it "still present". It
  reports an **advisory** finding naming the dropped command. Advisory, not
  error: adding a command (a stronger bar) is never flagged, only a removal, and
  the sweep surfaces it for a human rather than hard-blocking.
- **Acknowledging an intentional removal.** A legitimate rename or drop
  (`npm test` → `pnpm test`) would otherwise fire on every audit forever. List
  the removed command(s) under `verify_contract_ack`:

  ```json
  {"verify": "pnpm test", "verify_contract_ack": ["npm test"]}
  ```

  An acknowledged removal is **not fully silenced** — it is downgraded from an
  advisory finding to an **informational (`info`) line** (below advisory, never
  failing the audit) that still names the removed command and its ack. This is
  deliberate: `verify_contract_ack` lives on the *same gate-exempt
  `.agent/config.json` path the guard watches*, so the actor who deletes `verify`
  can add the dropped command to the ack list in the very same write. That means
  self-acknowledgment cannot be *prevented* — so the guarantee is **visibility,
  not prevention**: an acknowledged removal always leaves a visible line for a
  human to confirm. Only the named commands are downgraded; any *other* drop
  still fires at advisory severity.

Resilience and bounds: the very **first close** in a repo has no prior receipt,
so the sweep is clean by design — there is no baseline until one close has
recorded one, and because the comparison is per-risk, a risk class's own bar is
baselined only once that class has closed at least once (weakening a risk-keyed
command before its first close is not yet visible). The comparison is per-command
first line (cmd1), what the receipt records, so a change confined to lines 2+ of
a multi-line command is not distinguished; a clean sweep therefore means "no
first-line command was dropped", not "verify was never touched". A verify command
containing a literal backtick is not reliably tracked: the backtick-delimited
receipt encoding cannot represent it, so the recorded form is truncated at the
first backtick. Usually this over-flags (a persistent false advisory, clearable
with `verify_contract_ack`), but if a weakening removes exactly the text after
that first backtick the change can also be **missed**. Backticks in a `verify`
command are therefore unsupported for drift detection — use `$(…)` command
substitution instead; a lossless receipt encoding is future work. The comparison
is **set-based per risk**, so it flags a *dropped* command, not a **reordering**
(every command still runs, just in a new order — that is not a weakening the
sweep claims to catch). `verify_contract_ack` matches by command **string across
all risk classes** (it is not risk-qualified), so acknowledging a command's
removal accepts it wherever it was dropped. The sweep baselines off the **committed**
`## Verification Receipt` sections (which survive clones), not the gitignored
enforcement journal; neither the receipts, the journal, nor the ack list is
tamper-proof against an agent with raw filesystem access — nor against a
concurrent close of the *same task* overwriting a strong receipt with a weak one
— so this is best-effort *visibility*, and the OS sandbox plus human review
remain the real containment. The journal keeps the full per-close trail for
forensics.

## `.agent/models.json` — judge panel pins

Judge selection lives in `models.json`: the plugin ships defaults in `provider/models.json`, and each install can shadow them per key with a gitignored `.agent/models.json`. **Supported judge seats are Claude, Grok, and Codex** — live-verified 2026-08-22 (the pins `opus`, `sonnet`, `codex:gpt-5.6-terra:high`, `codex:gpt-5.6-sol:high`, and `grok:grok-4.6:high` all responded on the owner's account); Antigravity (`agy`) and Pi seats are experimental (see the [provider support matrix](providers.md#support-matrix)):

```json
{
  "default_judge": "claude",
  "panel": ["opus", "claude:claude-sonnet-5", "codex:gpt-5.6-terra:high", "grok:grok-4.6"],
  "aliases": {"opus": ["claude", "claude-opus-4-8", []]}
}
```

- `default_judge` — backend for bare `plan-review` / `impl-review`.
- `panel` — the judge seats for `panel-review`; each spec is `backend[:model[:effort]]`.
- `aliases` — shorthand names expanded before dispatch. Each value is a
  **three-element list** `[agent, canonical_model_or_null, [extra_args]]` (the
  same schema as `provider/models.json`); a bare string is off-schema and is
  silently dropped by the parser.

### Keeping pins alive

Pinned model ids rot as providers ship and retire models, so the pins have a maintenance loop:

- `tasks models check` audits every pin against **live availability**: codex pins are probed with a tiny prompt (the `~/.codex/models_cache.json` catalog alone doesn't prove your account can use a model), claude pins are probed budget-capped (claude has no list command — new ids enter via `--claude-candidates`), grok pins are checked against `grok models` (a login-aware entitlement list, so a listed pin is OK without a live turn), agy is unverifiable (`--model` is inert in `--print` mode; the judge always runs whatever model is selected in the agy UI). `--no-probe` is the free/fast degraded audit. Exits 1 when any pin can't run as configured.
- `tasks models detect [--json]` is the fast inventory: which agent CLIs are installed and each one's selectable models + reasoning-effort levels. It reads local caches and the cheap listing commands only (no model is live-probed — though `grok models` is login-aware, so not strictly offline), so it never proves availability the way `check` does — it lists *choices*. `/playbook:init` reads it to build the panel menu.
- `tasks models select` refreshes interactively: shows the report, takes the new panel + default judge, writes `.agent/models.json` — creating it on fresh installs and preserving keys it doesn't manage.
- `tasks models set --panel a,b --default-judge c [--force]` is the non-interactive twin — same validation and no-probe audit, flag-driven. A dead pin aborts unless `--force`; this is how `/playbook:init` persists the panel it asked you about.
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
| `PLAYBOOK_PANEL_QUORUM` | Overrides `panel_quorum` — same value grammar (`majority` / `all` / integer / fraction). |
| `PLAYBOOK_VERIFY_TIMEOUT_SECS` | Overrides `verify_timeout_secs` — the close-time verify ceiling (`0`/`unlimited` disables). |
| `PLAYBOOK_REVIEW_CONTEXT_CHARS` | Overrides `review_context_chars` — the argv-transport judge context budget. |
| `PLAYBOOK_REVIEW_CONTEXT_CHARS_STDIN` | Overrides `review_context_chars_stdin` — the stdin-transport judge context budget. |
| `PLAYBOOK_ALLOW_DANGEROUS` | Set truthy to acknowledge one destructive command past the `command_guard` interlock (a human-confirmed one-off). |
| `PLAYBOOK_BASH` | Absolute path to the `bash` the shell-dependent surfaces (audit sweeps, `merge-verify`, `scripts/verify`) should use. Needed only where a bare `bash` on `PATH` is not the right one — most often on Windows, where `bash.exe` in System32 is the WSL launcher rather than Git Bash. The chosen bash is probed with a sentinel; an unusable one fails closed. `$PLAYBOOK_VERIFY_BASH` (named for the dev verifier, exported by CI) is honoured as a fallback when `PLAYBOOK_BASH` is unset. |
| `PLAYBOOK_PROJECT_ROOT`, `PLAYBOOK_SESSION_ID`, `PLAYBOOK_SANDBOXED`, `PLAYBOOK_MINDMAP_MAX`, `PLAYBOOK_EVAL_CONFIG` | Internal — set by the wrappers, hooks, and sandbox; not meant to be set by hand. |
