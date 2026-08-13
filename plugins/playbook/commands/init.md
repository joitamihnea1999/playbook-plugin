---
description: Initialize or upgrade a project for playbook workflow
argument-hint: "[project name]"
allowed-tools: [Read, Write, Edit, Bash, Glob, Grep]
---

# Playbook Init

Initialize this project for playbook-managed workflow. Safe to re-run (idempotent) — upgrades template sections without losing project-specific content.

**Project name:** $ARGUMENTS (use the directory name if not provided)

## Instructions

Perform **every** step in order.

### 1. Run mechanical setup

Find and run the plugin's `scripts/init` script, which handles: `.claude/settings.json` permissions, `.agent/tasks/` directory, `MIND_MAP.md` stub, `.claude/bin/` wrappers, **CLAUDE.md** (created from the template, or template-owned sections merged in place — project content is preserved byte-for-byte), and **.gitignore** (a marker-guarded playbook runtime-state block). Resolve it from the install manifest first (the same copy the harness hooks run — a bare `find` can pick a stale cached version), falling back to a deterministic find:

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

### 2. Review CLAUDE.md and enrich it

The base write already happened mechanically in step 1 (create-or-merge; a pre-existing CLAUDE.md keeps every byte of project-specific content, and template-owned sections are updated in place). Your job is the part that requires intelligence:

- Read the merged CLAUDE.md. If the mechanical merge left anything semantically off — e.g. the project's own rules now duplicate or contradict a template section — reconcile it, keeping the project's intent.
- Add project-specific content the template cannot know: what the project is, its verify command in `.agent/config.json`, domain rules. Project content belongs in its own sections, not inside template-owned ones (those are refreshed on upgrade).
- Check `.gitignore`: the playbook runtime-state block is present; add language/tooling ignores the project needs (`__pycache__/`, `node_modules/`, …) — those are deliberately not mechanical.

### 3. Generate mind map if stub

If `MIND_MAP.md` contains only a stub (just a `# Mind Map` heading with no real content), run `/mindmap` to generate it from the codebase.

If it already has substantive content, leave it alone.

### 4. Verify

Run `.claude/bin/tasks bootstrap` to verify everything works. Report what was created or updated.
