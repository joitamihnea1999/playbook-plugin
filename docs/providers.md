# Providers

Playbook ships adapters for five agent CLIs. Each adapter can, in code, drive tasks as the **main agent** (under the same hooks) and/or serve as a **judge** on the review panel — but *support* is scoped by capability. As of the 2026-08-21 owner decision:

- **Claude Code** — supported as both the **main agent** and a **judge**. It is the only provider whose hooks gate a live coding agent in supported use.
- **Grok** and **Codex** — supported as **judges** (live-verified 2026-08-22). Their main-agent *enforcement* paths ship but are **experimental** (adapter code retained, no support claim, no live evidence).
- **Antigravity** and **Pi** — **experimental** everywhere. The adapter code ships and is retained, but there is no support claim and no live evidence.

## Support matrix

Legend: ✅ supported · ⚗ experimental (code ships, no support claim).

| Provider | CLI | Launcher | Main agent (enforcement) | Judge (review panel) |
|---|---|---|---|---|
| Claude Code | `claude` | *(native)* | ✅ supported | ✅ supported |
| Grok | `grok` | `playbook-grok` | ⚗ experimental | ✅ supported (live-verified 2026-08-22) |
| Codex | `codex` | `playbook-codex` | ⚗ experimental | ✅ supported (live-verified 2026-08-22) |
| Antigravity | `agy` | `playbook-agy` | ⚗ experimental | ⚗ experimental |
| Pi | `pi` | `playbook-pi` | ⚗ experimental | ⚗ experimental |

Agent-mode (hook enforcement) is supported on **Claude only**. Judge-seat support covers **Claude, Grok, and Codex**: on 2026-08-22 an owner live-check on Linux confirmed that the pins `opus`, `sonnet`, `codex:gpt-5.6-terra:high`, `codex:gpt-5.6-sol:high`, and `grok:grok-4.6:high` all respond on the owner's account. Antigravity and Pi keep their adapter code but carry no support claim and no live evidence.

## Provider notes

The support tier above governs the claim; the notes below describe the shipped mechanism, which is retained in all cases.

- **Claude Code** (`claude`, native) — reference platform; hooks registered by the plugin. Only judge with a budget cap (`judge_budget_usd`).
- **Codex** (`codex`, `playbook-codex`) — **judge: supported** (live-verified 2026-08-22). **Main agent: experimental** — `apply_patch` edits are gated via dedicated codex hooks (codex pre-blocks `apply_patch` edits but not file writes made through plain shell commands). Effort levels `low…ultra` per the model cache. Business-plan runs can be slow — raise the review timeout if judges expire.
- **Grok** (`grok`, `playbook-grok`) — **judge: supported** (live-verified 2026-08-22). **Main agent: experimental** — relies on always-trusted global hooks: `tasks init --provider grok` writes `~/.grok/hooks/playbook-enforcement.json` (task-gate + state-echo + chat-log), required on spaced project paths (iCloud) where project/plugin hooks never schedule. Restart Grok after install/upgrade. Project hooks still need `/hooks-trust` once. Payloads normalized by shared shim: camelCase keys, `Shell`→Bash, `StrReplace`→Edit, plus `write`→Write, `search_replace`→Edit, `run_terminal_command`→Bash. `grok models` is an account-entitlement list. Web search on by default (judges pass `--disable-web-search` when off).
- **Antigravity** (`agy`, `playbook-agy`) — **experimental everywhere.** The ex-`gemini` CLI. Judge prompts ride `--print <prompt>` (no stdin path). The CLI offers no usable model flag, so the judge always runs whatever model is selected in the agy UI — pins are unverifiable by probe (which is part of why it carries no support claim).
- **Pi** (`pi`, `playbook-pi`) — **experimental everywhere.** Ships a hook adapter (`playbook-pi-hook-adapter.ts`) and a local models file (`playbook-pi-omlx-models.json`). Windows argv-length guard for big judge prompts.

## Launchers

The `playbook-*` wrappers (installed to `.claude/bin/` by `/playbook:init`) start each CLI with a unique per-session Playbook session ID (PID-based, provider-agnostic), so gate state, chat-log attribution (`claude`/`codex`/`agy`/`grok`/`pi` tags), and multi-agent handoffs work identically everywhere. Each wrapper provisions its session directory in the [per-user lane](architecture.md#per-user-lanes) — the same one the hooks and the `tasks` CLI read — and refuses to launch rather than guess when a repo has lanes but no `.agent/current_user` marker. (The launchers exist for all four non-Claude providers; only their judge use on Grok and Codex is a supported claim.)

## How the same hooks run everywhere

The plugin registers six lifecycle hooks once (see [architecture](architecture.md)); non-Claude providers reach them through provider adapters (`provider/adapters/*.py`) plus, where needed, a payload-normalization shim that translates each CLI's event schema to the Claude one. The edit gate ("no active task → no code edits") *runs* under every provider in code, but **supported enforcement is Claude-only** — the grok and codex enforcement paths are experimental, and antigravity/pi are experimental everywhere. Two provider-specific caveats on the experimental paths: codex pre-blocks `apply_patch` edits but not file writes made through plain shell commands, and **Grok** relies on always-trusted `~/.grok/hooks/playbook-enforcement.json` (project/plugin hooks need `/hooks-trust` and still may not schedule on spaced paths).

## Judges across providers

Judge-seat support covers **Claude, Grok, and Codex** (live-verified 2026-08-22); Antigravity and Pi judge seats are experimental. Panel seats are specs like `codex:gpt-5.6-terra:high` or `grok:grok-4.6` in [`.agent/models.json`](configuration.md). Each provider adapter knows how to run its CLI headless (prompt on argv vs stdin, model/effort splitting, context inlining) and how to classify failures. Pin health is maintained with `tasks models check` / `select` — including probe-confirmed hard stops when a pinned model disappears from your account, which is a when, not an if.
