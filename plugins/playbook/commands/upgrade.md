---
description: Upgrade the playbook plugin to the latest version
allowed-tools: [Bash, Read, Edit]
---

# Upgrade Playbook Plugin

Upgrade the installed playbook plugin to the latest release.

## Instructions

Run these commands in sequence. Stop on any failure.

### 1. Check current version

```bash
cat ~/.claude/plugins/marketplaces/playbook-x-marketplace/plugins/playbook/.claude-plugin/plugin.json 2>/dev/null || echo "Not installed"
```

### 2. Remove old plugin

```bash
claude plugin marketplace remove playbook-x-marketplace
```

### 3. Re-add marketplace and install

```bash
claude plugin marketplace add mariuscristescu/playbook-plugin
claude plugin install playbook@playbook-x-marketplace
```

### 4. Run /playbook:init to update project files

Run `/playbook:init` to merge any new CLAUDE.md sections and update project wrappers, hooks, and `.gitignore`. This is safe to re-run — it's idempotent. (Note: the plugin's initializer is `/playbook:init`, which runs `scripts/init`; Claude Code's built-in `/init` is a different, generic CLAUDE.md generator that does none of this mechanical upgrade work.)

### 5. Verify

```bash
cat ~/.claude/plugins/marketplaces/playbook-x-marketplace/plugins/playbook/.claude-plugin/plugin.json
```

Report the old and new version numbers.
