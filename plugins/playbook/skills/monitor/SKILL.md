---
name: monitor
description: >
  Conversation monitor — a second Claude agent that watches the front agent's
  work from outside and nudges when trajectory goes wrong. Reads session JSONL
  incrementally, maintains a compact judgment trace, delivers nudges via hook.
argument-hint: [start]
---

# Monitor

## What It Does

Launches a **conversation monitor** — a second Claude agent that watches your
current conversation from outside and injects steering nudges when it notices
the trajectory going wrong.

The monitor runs as its own process, reads the session JSONL incrementally,
maintains a compact judgment trace, and writes nudges that the front agent
sees via a `PostToolUse` hook.

## Why

LLMs collapse toward the user's frame across turns — mirroring vocabulary,
accepting premises, producing responses that feel like engagement but are
accommodation. Trajectory patterns are invisible at turn granularity. Seeing
them requires a vantage point outside the sequence.

Per-turn instructions can't fix this because they ask the drifting model to
monitor its own drift. An external monitor can watch the arc.

## Usage

**Start the monitor** (from the project, ideally after the front agent's first
tool call so the transcript binding is clean):
```
.claude/bin/monitor start
```
This runs `scripts/monitor-lib/launch-monitor`, which starts a second,
**sandboxed** Claude session (`claude --safe-mode`, so plugin hooks are disabled
inside it) contained by `provider.sandbox` — seatbelt on macOS, bwrap on Linux:
the project is bound read-only and the monitor's own `<agent-dir>/monitor/` is
the only project-side writable path. It auto-detects the oldest live front-agent
pid; override with `.claude/bin/monitor start --session-id pid-<N>`.

**Stop the monitor:** the monitor IS a Claude session, not a background daemon —
end it the way you end any session (tell it "stop", or exit that Claude). There
is no `/monitor off` command and no `monitor.py` process to signal.

## What the Monitor Watches

- **User messages** — what you asked for, how you corrected the agent
- **Agent text** — compact previews of what the agent said
- **Tool calls with phase tags** — orient (O), execute (E), verify (V), meta (M)
- **Work spans** — sequences of tool calls between user messages

The sensor extracts all this incrementally from `~/.claude/projects/<slug>/<session>.jsonl`.

## What the Monitor Writes

A **flat** layout under `<agent-dir>/monitor/` (`.agent/monitor/`, or
`.agent/<user>/monitor/` on a multi-user repo) — the monitor's only writable
area (there is no `pids/<session-id>/` subdir):
- `session.md` — wake-by-wake journal (compact trace + judgment)
- `nudge.md` — one-sentence outbox consumed by the hook
- `trace.md` — sensor-written compact event trace
- `rules.md` — steering rules learned from observation
- `MONITOR_MIND_MAP.md` — accumulated orientation knowledge

## Delivery

The monitor writes nudges to `<agent-dir>/monitor/nudge.md`.
A non-plugin `PostToolUse` hook (registered in `.claude/settings.json`) reads
the nudge on the front agent's next tool call, emits it as `additionalContext`,
and logs `[MONITOR→<session-id>]` to chat_log.

**Note:** UserPromptSubmit `additionalContext` is broken in current CC
(plugin and non-plugin hooks affected, issue #12151). PostToolUse is the
working injection point. Nudges arrive on the agent's next tool call, not
between user message and LLM response.

## When It Nudges

Silence is the default. Most turns need no intervention.

The monitor nudges when it sees:
- **Accommodation** — agent mirroring the user's frame without resistance
- **Gap blindness** — missing considerations nobody caught
- **Premature convergence** — closing before alternatives explored
- **Ground re-covering** — re-deriving what was already established
- **Phase imbalance** — orient-heavy without execute, edit-heavy without verify
- **Rule triggers** — patterns in `rules.md` firing

Rules start empty and accumulate from real observations. The monitor is not
pre-coded with problems you haven't seen.

## Lifecycle

1. `.claude/bin/monitor start` — launch the sandboxed monitor Claude session
2. `bootstrap.sh` seeds the briefing; each wake the sensor's `wait_once` blocks
   until the front agent finishes a turn (`stop_reason: end_turn`) or a 60s
   stall flush
3. Each wake: append to session.md, maybe write nudge.md, maybe add a rule
4. Stop by exiting that Claude session (a final session.md entry, then exit)

## Architecture

- `scripts/monitor-lib/launch-monitor` — starts the sandboxed monitor Claude session
- `scripts/monitor-lib/bootstrap.sh` — seeds the monitor's briefing on each start
- `scripts/monitor-lib/sensor.py` — incremental JSONL reader + compact extractor
  (`read_new_events`, `wait_once`)
- `scripts/monitor-lib/CLAUDE.md` — the monitor's operating instructions
- `.claude/hooks/monitor-nudge.sh` — injection hook (registered in settings.json)
- `<agent-dir>/monitor/` — the monitor's home (r/w for the monitor, r/o everywhere else)

## Limitations (v1)

- Claude JSONL only (Codex extension deferred)
- PostToolUse injection timing (not UserPromptSubmit) — upstream bug
- `rules.md` starts empty and fills slowly
- Single-project scope (cross-project memory deferred)
