---
name: readme-audit
description: Maintainer-only audit of the playbook plugin's shipped surface (commands, skills, CLI subcommands, providers, hooks, config) against README.md and docs/, followed by a user-vetted update and a new drift baseline. Run when doctor/bootstrap warns of README drift, after feature tasks land, or before a release.
---

# README Audit — maintainer skill

**Scope:** the playbook-plugin source repo ONLY (the repo containing `plugins/playbook/.claude-plugin/plugin.json`, `README.md`, and `.git`). This file is intentionally OUTSIDE `plugins/playbook/` so it never ships to plugin users — invoke it by path, don't look for it in the installed plugin.

**Contract:** nothing lands in README.md, docs/, or CHANGELOG.md without an explicit user decision recorded in the vetting ledger. The audit ends by writing a new baseline; the baseline is what `tasks doctor` / `tasks bootstrap` compare against when deciding to nag.

**Cost note:** full run with hybrid vetting ≈ 17–30K output tokens (2–3 rounds). Incremental runs are cheaper — prefer them when a baseline exists.

---

## Step 0 — Preflight (fail loud, never silently under-report)

Resolve `REPO` = the plugin source repo root: **the git root three levels above this SKILL.md's own location** (`<REPO>/.claude/skills/readme-audit/SKILL.md`). If you were invoked by path, that path tells you `REPO`; verify with `git -C "$REPO" rev-parse --show-toplevel`. Do NOT assume `REPO` = cwd — maintenance sessions usually sit in the dogfood *workspace*, one level above the checkout.

Verify ALL of the following exist — if ANY is missing, STOP and report which one; do not continue with a partial inventory:

```bash
test -f "$REPO/plugins/playbook/.claude-plugin/plugin.json"   # plugin manifest
test -d "$REPO/plugins/playbook/commands"                     # slash commands
test -d "$REPO/plugins/playbook/skills"                       # skills
test -d "$REPO/plugins/playbook/provider/adapters"            # provider adapters
test -f "$REPO/plugins/playbook/hooks/hooks.json"             # hook registrations
test -f "$REPO/plugins/playbook/tasks/cli.py"                 # CLI dispatch
test -f "$REPO/README.md"
git -C "$REPO" rev-parse HEAD                                 # must be a git checkout
```

These paths are this skill's ground truth. If the repo layout ever changes, FIX THIS FILE in the same task — a glob that quietly matches nothing recreates the drift this skill exists to kill.

Mode select: if `$REPO/docs/readme-audit-baseline.json` exists AND is committed clean (`git -C "$REPO" status --porcelain docs/readme-audit-baseline.json` prints nothing) → **incremental mode** (Step 1b). If it exists but is untracked/dirty, a previous landing crashed between the baseline write and its commit — finish Step 4's baseline commit first, then re-select. Else → **full mode** (Step 1a).

## Step 1a — Inventory (full mode): enumerate from live sources only

Never write an inventory row from memory or from a previous audit — every row cites the command that produced it.

```bash
ls "$REPO/plugins/playbook/commands/"*.md                     # slash commands (+ head -4 for descriptions)
ls -d "$REPO/plugins/playbook/skills/"*/                      # skill bundles
ls "$REPO/plugins/playbook/provider/adapters/"*.py            # providers (ignore __init__/__pycache__)
grep -nE '^\s+(el)?if cmd (==|in)' "$REPO/plugins/playbook/tasks/cli.py"   # CLI subcommands
ls "$REPO/plugins/playbook/scripts/" | grep '^playbook-'      # provider wrappers (NOTE: also matches pi support files *.ts/*.json — launchers are the extensionless ones)
for f in "$REPO/plugins/playbook/scripts/"*; do [ -f "$f" ] && [ -x "$f" ] && basename "$f"; done   # ALL shipped executables (init, sandbox, monitor, hook scripts…), not just wrappers
python3 -c "import json;h=json.load(open('$REPO/plugins/playbook/hooks/hooks.json'));import re;print(sorted({c for v in (h.get('hooks',h)).values() for e in v for hh in e.get('hooks',[e]) for c in [hh.get('command','')] if c}))"   # hook command targets
grep -rhoE 'PLAYBOOK_[A-Z_]+' "$REPO/plugins/playbook/tasks/" "$REPO/plugins/playbook/provider/" "$REPO/plugins/playbook/scripts/" 2>/dev/null | sort -u
grep -rhoE 'load_config\([^)]*\)\.get\("[a-z_]+"\)' "$REPO/plugins/playbook/tasks/" | grep -oE '"[a-z_]+"' | sort -u   # config.json keys, from the actual read sites — no hardcoded key filter
python3 -c "import json;print(json.load(open('$REPO/plugins/playbook/.claude-plugin/plugin.json'))['version'])"
```

Then cross-check EVERY enumerated item against `README.md` + every `docs/*.md` page + `CHANGELOG.md`, and record a gap table: `item | source command | where documented | status: absent / stale / ok`. "Stale" means documented but contradicting the code (wrong count, wrong flag, dead link, renamed model/provider). Also verify the inverse: every claim in README/docs still has a live counterpart (catches removed features).

## Step 1b — Inventory (incremental mode)

Read the baseline, then diff only what moved:

```bash
python3 - "$REPO" << 'EOF'
import json, subprocess, sys
repo = sys.argv[1]
b = json.load(open(f"{repo}/docs/readme-audit-baseline.json"))
out = subprocess.run(
    ["git", "-C", repo, "log", "--oneline", f"{b['audited_commit']}..HEAD",
     "--", *b["covered_paths"]],
    capture_output=True, text=True)
print(out.stdout or f"(empty — docs current as of v{b['version']})")
print(out.stderr, file=sys.stderr)
EOF
```

If the commit range is empty → report "docs current as of <version>", refresh the baseline date, DONE.
Otherwise run Step 1a's enumeration but scope the gap table to items touched by those commits (plus any README/docs claims those commits invalidate). If the baseline commit is unreachable (rebase), say so and fall back to full mode.

## Step 2 — Proposal: layered, user-first

Structure (decided with the user, task 017):

- **README.md — budget ~130 lines.** Audience: someone discovering the plugin. Keeps: what/why narrative, install, quickstart usage, feature tour (short), fork notice, the two images, a quick-nav line linking every docs/ page. The narrative voice of the existing README is a feature — edit for coverage, don't flatten the prose.
- **docs/cli.md** — full tasks-CLI subcommand reference (all of them, including merge/retro/internal tooling, marked as such).
- **docs/configuration.md** — config.json, models.json, env vars, budgets/timeouts, precedence rules.
- **docs/providers.md** — the 5-provider matrix (claude/codex/agy/grok/pi), wrappers, judge panel mechanics.
- **docs/architecture.md** — hooks, enforcement/deny-list, monitor, sandbox, task-system internals.
- **CHANGELOG.md** — keep-a-changelog style; the audit adds one entry per audited version range (features shipped since last audit — the gap table's "absent" rows are exactly this list).
- README footer stamp: `<!-- readme-audit: vX.Y.Z @ YYYY-MM-DD -->` (machine-checkable, invisible on GitHub; a sha can't be known before its own commit, so the stamp carries version + date — the baseline JSON carries the sha).

Rules: every docs/ page reachable from README via relative link; one Diátaxis mode per page (reference OR explanation, don't mix); no feature documented in two places (link instead); internal-only surface (e.g. hook shims, PLAYBOOK_* internals) may be deliberately undocumented — record that as a decision, not an omission.

## Step 3 — Vetting (hybrid, ledger-backed)

Vetting units = the NEW structure: each proposed README section, each docs/ page, the CHANGELOG entry, plus a **disposition map** (one table: every section of the CURRENT README → kept / moved to docs/X / dropped). Old→new diffs are not the medium — the restructure makes them meaningless.

Ledger: `<task dir>/vetting-ledger.json` — append-only decisions:

```json
{"unit": "readme/install", "hash": "<sha256 of proposed text>", "decision": "accept|revise|drop",
 "round": 1, "note": "<user's revise instruction, if any>"}
```

Protocol, per round:
1. Publish/update ONE artifact showing every UNRESOLVED unit's full proposed text (+ the disposition map in round 1). Accepted units are FROZEN: excluded from the artifact and from regeneration. If a frozen unit's text must change (e.g. a cross-link), it re-enters the round as unresolved with a new hash.
2. Collect decisions via AskUserQuestion — max 4 questions per call, max 4 options each; previews carry the section text. "Revise" details arrive via the Other/notes field or the next chat message; record them in the ledger `note`.
3. Write each decision to the ledger AS IT LANDS (the ledger is the crash/compaction resume point — on resume, re-derive the unresolved set from it).
4. Loop until every unit has a decision whose `hash` matches the current proposed text. **Default-deny:** a unit with no current-hash decision does not land, period.

## Step 4 — Landing

On a feature branch (never push — the maintainer reviews and pushes):
1. Write approved README.md, docs/*.md, CHANGELOG.md exactly as accepted (byte-for-byte vs the hashed text).
2. Link check: every relative link in README/docs resolves on the filesystem; external URLs curl-checked best-effort (record failures, don't block on flaky remotes).
3. Write `$REPO/docs/readme-audit-baseline.json` — if this write fails, the run FAILED; say so:

```json
{"audited_commit": "<git rev-parse HEAD after the docs commit>",
 "version": "<plugin.json version>", "date": "YYYY-MM-DD",
 "covered_paths": ["plugins/playbook/commands", "plugins/playbook/skills",
                   "plugins/playbook/provider", "plugins/playbook/tasks",
                   "plugins/playbook/hooks", "plugins/playbook/scripts"]}
```

4. Commit in TWO steps (the baseline records a commit sha, which cannot be known inside its own commit): first commit the docs + any code from the same task → sha X; then write the baseline with `audited_commit: X` and commit it separately. The baseline commit touches only `docs/`, which is outside `covered_paths`, so it can never trigger its own drift warning. `git diff --stat` of both commits together must match the approved scope exactly — anything else is an error.
5. Report: units landed, units dropped, link-check result, observed vetting cost (payload sizes per round).

## Deviations

If executing this skill forces an improvisation (a glob that no longer matches, a vetting situation the protocol doesn't cover), STOP, fix this SKILL.md in the same task, and note the amendment — the skill text is the procedure of record, not a suggestion.
