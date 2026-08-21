#!/bin/bash
# monitor-nudge.sh — hook for injecting monitor nudges into agent context
#
# Registered as a NON-PLUGIN hook in .claude/settings.json.
# Works for both PostToolUse and UserPromptSubmit (reads event name from stdin).
# Current CC: PostToolUse additionalContext works, UserPromptSubmit is broken (bug #12151).
#
# Reads <agent-dir>/monitor/nudge.md (T121 flat layout), where <agent-dir> is
# the per-user lane `.agent/<user>/` when the repo declares one, else `.agent/`.
# The monitor WRITES its nudge into that same lane (launch-monitor resolves
# MONITOR_DIR the same way), so a root-only reader here would leave every nudge
# undelivered on a multi-user repo.
# If non-empty: atomic claim, emit additionalContext, log to chat_log.
# If empty or missing: exit 0 silently (no-op).

set -e

# Skip when invoked from the monitor's own claude session — otherwise the
# monitor's own Bash/Write tool calls would trigger this hook, which would
# consume the monitor's just-written nudge before the front agent ever sees
# it. Launch-monitor exports PLAYBOOK_ROLE=monitor specifically for this.
if [ "${PLAYBOOK_ROLE:-}" = "monitor" ]; then
    exit 0
fi

# Root + lane resolution are inlined rather than sourced from
# scripts/gate-echo-lib.sh: this file is registered as a NON-plugin hook and is
# copied into the project's own .claude/hooks/, so CLAUDE_PLUGIN_ROOT is unset
# and the plugin's scripts/ dir is at no fixed relative path. The contract is
# identical to that library (and to tasks/core.py and provider/paths.py);
# tests/wrapper-multiuser-fixture.sh asserts this copy agrees with them.

# Find project root: legacy `.agent/tasks/` OR multi-user `.agent/<user>/tasks/`.
find_root() {
    local dir="$PWD" sub
    while [ "$dir" != "/" ]; do
        [ -d "$dir/.agent/tasks" ] && echo "$dir" && return
        if [ -d "$dir/.agent" ]; then
            for sub in "$dir/.agent"/*/; do
                [ -d "${sub}tasks" ] && echo "$dir" && return
            done
        fi
        dir="$(dirname "$dir")"
    done
}

PROJECT_DIR=$(find_root)
[ -z "$PROJECT_DIR" ] && exit 0

# Resolve this user's lane. An unusable marker means we cannot tell whose nudge
# outbox to read: exit silently rather than read the shared root. Never fail the
# hook — a nudge is advisory, and taking down every tool call over a bad marker
# would be far worse than a missed nudge.
AGENT_DIR="$PROJECT_DIR/.agent"
if [ -f "$PROJECT_DIR/.agent/current_user" ]; then
    CU=""; CU_EXTRA=""
    # One-line contract — see gate-echo-lib.sh resolve_agent_dir.
    { read -r CU; read -r CU_EXTRA; } < "$PROJECT_DIR/.agent/current_user" 2>/dev/null || true
    CU="${CU%$'\r'}"
    [ -n "$CU_EXTRA" ] && exit 0
    case "$CU" in
        ""|"."|"..") exit 0 ;;
        [a-zA-Z0-9]*) ;;
        *) exit 0 ;;
    esac
    case "$CU" in
        *[!a-zA-Z0-9_.-]*) exit 0 ;;
    esac
    AGENT_DIR="$PROJECT_DIR/.agent/$CU"
fi

# Read stdin to extract hook_event_name (PostToolUse or UserPromptSubmit)
INPUT=$(cat)
export EVENT_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read() or '{}').get('hook_event_name','UserPromptSubmit'))" 2>/dev/null || echo "UserPromptSubmit")

SESSION_ID="${PLAYBOOK_SESSION_ID:-pid-$PPID}"
NUDGE_FILE="$AGENT_DIR/monitor/nudge.md"

# No nudge file or empty — silent exit
[ -f "$NUDGE_FILE" ] || exit 0
[ -s "$NUDGE_FILE" ] || exit 0

# Atomic claim: mv to .delivering so monitor can't overwrite mid-read
DELIVERING="$NUDGE_FILE.delivering"
mv "$NUDGE_FILE" "$DELIVERING" 2>/dev/null || exit 0

# Read content
NUDGE_CONTENT=$(cat "$DELIVERING")

# Skip if content is empty after read (drop the claimed empty file)
[ -z "$NUDGE_CONTENT" ] && { rm -f "$DELIVERING"; exit 0; }

# Emit additionalContext — this is what gets injected into the agent's context.
# The nudge was already CLAIMED (moved) above, so a failed emit (e.g. python3
# missing) used to consume-and-drop it under `set -e`. Build the JSON first, and
# only clear the claim on a SUCCESSFUL emit; on failure RESTORE the file so a
# later session can still deliver it — a nudge is delivered at-least-once, never
# silently lost. (stderr, not stdout, would carry any diagnostic — but here we
# simply leave the nudge for next time.)
if NUDGE_JSON=$(python3 -c "
import json, sys, os
nudge = sys.stdin.read().strip()
event = os.environ.get('EVENT_NAME', 'UserPromptSubmit')
out = {
    'hookSpecificOutput': {
        'hookEventName': event,
        'additionalContext': '[MONITOR] ' + nudge
    }
}
print(json.dumps(out))
" <<< "$NUDGE_CONTENT" 2>/dev/null) && [ -n "$NUDGE_JSON" ]; then
    printf '%s\n' "$NUDGE_JSON"
    rm -f "$DELIVERING"
else
    mv "$DELIVERING" "$NUDGE_FILE" 2>/dev/null || true
    exit 0
fi

# Log to chat_log
LOCAL_LOG="$AGENT_DIR/chat_log.md"
if [ -f "$LOCAL_LOG" ]; then
    TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
    {
        echo "---"
        echo ""
        echo "**[MONITOR→$SESSION_ID]** [$TIMESTAMP] ($EVENT_NAME)"
        echo ""
        echo "$NUDGE_CONTENT"
        echo ""
    } >> "$LOCAL_LOG"
fi
