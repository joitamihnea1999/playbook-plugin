---
description: Initialize or upgrade a project for playbook workflow
argument-hint: "[project name]"
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep, AskUserQuestion]
---

# Playbook Init

Initialize this project for playbook-managed workflow. Safe to re-run (idempotent) — upgrades template sections without losing project-specific content.

**Project name:** $ARGUMENTS (use the directory name if not provided)

## Instructions

Perform **every** step in order.

### 1. Run mechanical setup

Find and run the plugin's `scripts/init` script, which handles: `.claude/settings.json` permissions, `.agent/tasks/` directory, `MIND_MAP.md` stub, `.claude/bin/` wrappers, **CLAUDE.md** (created from the template, or template-owned sections merged in place — project content is preserved byte-for-byte), and **.gitignore** (a marker-guarded playbook runtime-state block). It also seeds `.agent/config.json` with the review knobs and the strict policy defaults (`panel_required_for: "all"`). Resolve it from the install manifest first (the same copy the harness hooks run — a bare `find` can pick a stale cached version), falling back to a deterministic find:

```bash
INIT_SCRIPT="$(python3 - "$PWD" 2>/dev/null <<'PY'
import glob, json, os, sys
def vkey(v):
    return tuple(int(x) if x.isdigit() else -1 for x in str(v).split("."))
def same_dir(a, b):
    try: return os.path.samefile(a, b)
    except OSError: return os.path.realpath(a) == os.path.realpath(b)
try:
    project = sys.argv[1] if len(sys.argv) > 1 else ""
    root = os.path.expanduser("~/.claude/plugins")
    cands = []  # (rank, negated-version, path) — plain ascending sort wins
    try:
        data = json.load(open(root + "/installed_plugins.json"))
        for k, v in (data.get("plugins") or {}).items():
            if k.split("@")[0] != "playbook": continue
            for e in v or []:
                s = (e.get("installPath") or "") + "/scripts/init"
                if not os.access(s, os.X_OK): continue
                pp = e.get("projectPath") or ""
                if pp and not (project and same_dir(pp, project)): continue
                cands.append((0 if pp else 1, tuple(-x for x in vkey(e.get("version"))), s))
    except Exception: pass
    if not cands:
        for s in glob.glob(root + "/cache/*/playbook/*/scripts/init"):
            if os.access(s, os.X_OK):
                ver = os.path.basename(os.path.dirname(os.path.dirname(s)))
                cands.append((2, tuple(-x for x in vkey(ver)), s))
        for pat in ("/marketplaces/*/plugins/playbook/scripts/init", "/cache/*/playbook/scripts/init"):
            for s in glob.glob(root + pat):
                if os.access(s, os.X_OK): cands.append((3, (), s))
    if cands: print(sorted(cands)[0][2])
except Exception: pass
PY
)"
[ -z "$INIT_SCRIPT" ] && INIT_SCRIPT="$(find ~/.claude/plugins -path '*/playbook/scripts/init' -type f 2>/dev/null | sort | head -1)"
if [ -z "$INIT_SCRIPT" ]; then
    echo "Error: playbook plugin not found." >&2
    exit 1
fi
bash "$INIT_SCRIPT" "<project name>"
```

Check the output. If it reports any failures, stop and fix before continuing.

### 2. Choose the judge panel (interactive — this is the one real per-machine choice)

Everything else in playbook is set to the single correct value (the panel checks **all** closes, merges only ever run via `/playbook:merge`, standing gates stay at their optimal defaults). The one thing that genuinely varies per machine is **which agent CLIs are installed** — so this is where you ask.

1. **Detect what's available** (fast, no network):

   ```bash
   .claude/bin/tasks models detect
   ```

   This lists every installed agent CLI (claude / codex / agy / grok / pi) with its selectable models and — for codex and grok — the reasoning-effort levels each accepts.

2. **Ask the user, in the conversation, how to build the panel.** Use `AskUserQuestion`. Ground the options in the `detect` output — offer only models from agents that are actually installed. Two questions:
   - **Panel members** (multi-select): which models sit on the review panel. A panel exists so no single model rubber-stamps itself — recommend **at least two, ideally from different vendors** when more than one is installed. Each pick becomes a judge spec: `provider:variant[:effort]` (e.g. `codex:gpt-5.5:high`, `grok:grok-4.6:medium`), a bare provider, or a shipped alias (`opus`, `sonnet`, `fable`, `haiku`). For codex/grok picks, also ask the **effort** from the levels `detect` reported for that model.
   - **Default judge**: the single backend for plan/impl single-judge reviews (`tasks plan-review` / `impl-review`). Default to `opus`.

   **Recommended default if the user has no preference or only Claude is installed:** panel `opus, sonnet`, default judge `opus` — the shipped all-Claude baseline. In that case you may skip writing a file (the shipped default already is exactly this); only write when the user chooses something.

3. **Offer an optional live availability check** before committing (fast-detect above only reads local caches — a codex model listed there can still 400 for this account). Ask the user whether to verify the chosen panel actually runs; it launches each CLI once and can take a minute or two.

4. **Write the choice:**

   ```bash
   .claude/bin/tasks models set --panel "<spec1,spec2,...>" --default-judge "<spec>"
   ```

   `set` validates every spec and refuses a panel with a dead pin (re-run with `--force` only if the user insists). If the user opted into the live check in step 3, run it now and report the verdicts; a `GONE` pin means re-pick:

   ```bash
   .claude/bin/tasks models check
   ```

**Non-interactive fallback:** if you genuinely cannot ask the user (headless run), leave the shipped all-Claude defaults in place — do not write `models.json`. Enforcement and reviews work on the defaults.

### 3. Establish the verify command (so `tasks work done` can actually prove "green")

Closing a task runs the project's declared `verify` command; if none is declared, close loudly refuses to claim it verified. `scripts/init` does **not** guess one, so set it now. Verify must run **everything** — typecheck **and** tests **and** lint — not a subset.

1. **Detect the project's checks.** Inspect the repo for its real tooling and assemble a single command that runs *all* of them, chained with `&&`. Look for, e.g.:
   - Python: `pyproject.toml` / `setup.cfg` → `pytest`, `mypy`/`pyright`, `ruff`/`flake8` (or `python3 -m unittest discover` if that is how the suite runs).
   - Node: `package.json` scripts → `npm test`, `npm run typecheck`/`tsc --noEmit`, `npm run lint`.
   - Make/just: a `test`/`check`/`lint` target in `Makefile`/`justfile`.
   - Rust: `cargo test && cargo clippy -- -D warnings`. Go: `go test ./... && go vet ./...`.

2. **Confirm with the user.** Show the assembled command and ask (via `AskUserQuestion` or plainly) whether it runs everything or if a check is missing. The confirm step exists to catch a **missed** check — not to let verification be narrowed. Correct it per their answer.

3. **Write it into `.agent/config.json`** under the `verify` key (a string that runs everything). Read the file, add/replace `verify`, keep every other key. Example result:

   ```json
   { "...": "...", "verify": "python3 -m pytest && mypy . && ruff check ." }
   ```

   If the project is a brand-new empty repo with no checks yet, say so and leave `verify` unset rather than inventing a command — note that close will refuse until it is set.

### 4. Review CLAUDE.md and enrich it

The base write already happened mechanically in step 1 (create-or-merge; a pre-existing CLAUDE.md keeps every byte of project-specific content, and template-owned sections are updated in place). Your job is the part that requires intelligence:

- Read the merged CLAUDE.md. If the mechanical merge left anything semantically off — e.g. the project's own rules now duplicate or contradict a template section — reconcile it, keeping the project's intent.
- Add project-specific content the template cannot know: what the project is, domain rules. Project content belongs in its own sections, not inside template-owned ones (those are refreshed on upgrade).
- Check `.gitignore`: the playbook runtime-state block is present; add language/tooling ignores the project needs (`__pycache__/`, `node_modules/`, …) — those are deliberately not mechanical.

### 5. Generate mind map if stub

If `MIND_MAP.md` contains only a stub (just a `# Mind Map` heading with no real content), run `/mindmap` to generate it from the codebase.

If it already has substantive content, leave it alone.

### 6. Verify

Run `.claude/bin/tasks bootstrap` to verify everything works. Report what was created or updated — including the panel you configured and the verify command you set.
